"""Provider contract, retry safety, and webhook persistence regressions."""

import base64
import hashlib
import hmac
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.notification_delivery import decrypt_snapshot, encrypt_snapshot, send_line, verify_signature


def test_webhook_verifies_exact_original_bytes():
    body = b'{"destination":"bot-id", "events":[]}'
    signature = base64.b64encode(hmac.new(b"secret", body, hashlib.sha256).digest()).decode()
    assert verify_signature(body, signature, "secret")
    assert not verify_signature(body + b" ", signature, "secret")
    assert not verify_signature(body, signature, "wrong-secret")
    assert not verify_signature(body, "\u00ff", "secret")


def test_line_timeout_is_unknown_and_retry_keeps_same_key_and_bytes():
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("simulated response loss", request=request)
        return httpx.Response(409, headers={"x-line-accepted-request-id": "original-request"})

    settings = SimpleNamespace(line_channel_access_token="test-token")
    transport = httpx.MockTransport(handler)
    payload = {"to": "test-user", "messages": [{"type": "text", "text": "test"}]}
    first = send_line(payload, "fixed-retry-key", settings, transport=transport)
    second = send_line(payload, "fixed-retry-key", settings, transport=transport)
    assert first.uncertain and first.retryable and first.result == "UNKNOWN"
    assert second.result == "API_ACCEPTED"
    assert second.accepted_request_id == "original-request"
    assert requests[0].headers["X-Line-Retry-Key"] == requests[1].headers["X-Line-Retry-Key"]
    assert requests[0].content == requests[1].content


def test_arbitrary_409_is_not_false_acceptance():
    settings = SimpleNamespace(line_channel_access_token="test-token")
    result = send_line({}, "key", settings, transport=httpx.MockTransport(lambda _: httpx.Response(409)))
    assert result.result == "FAILED"


def test_line_without_credentials_cannot_fake_delivery():
    result = send_line({}, "key", SimpleNamespace(line_channel_access_token=None))
    assert result.result == "FAILED"
    assert result.error_code == "LINE_NOT_CONFIGURED"


def test_notification_snapshot_is_encrypted_at_rest():
    payload = {"to": "sensitive-line-subject", "messages": [{"type": "text", "text": "hello"}]}
    snapshot = encrypt_snapshot(payload, "test-secret")
    assert "sensitive-line-subject" not in json.dumps(snapshot)
    assert decrypt_snapshot(snapshot, "test-secret") == payload


@pytest.fixture
def notification_api(tmp_path):
    from delivery_support import delivery_environment
    client, factory, settings, engine = delivery_environment(tmp_path)
    with client:
        yield client, factory, settings
    engine.dispose()


def _seed_notification(factory, *, purpose="CASE_SUBMITTED", task=False):
    from app.db import utcnow
    from app.models import Case, DomainEvent, IdentityLink, Task
    from app.notifications import enqueue_notification
    with factory() as db:
        db.get(Case, "case").status = "UNDER_REVIEW"
        db.add(IdentityLink(id="line-link", account_id="owner", provider_id="provider", line_subject="line-subject", binding_version=1))
        db.add(DomainEvent(id="event", case_id="case", event_type=purpose, resource_id="case", resource_version=1))
        if task:
            db.add(Task(id="task", case_id="case", status="OPEN", current_revision_no=1,
                        title="Required", requirement="Proof", due_at=utcnow() + timedelta(days=2)))
        db.flush()
        rows = enqueue_notification(db, event_id="event", case_id="case", account_id="owner", purpose=purpose,
                                    task_id="task" if task else None, task_revision=1 if task else None)
        db.commit()
        return {row.channel: row.id for row in rows}


def test_persistent_worker_recovers_unknown_without_new_key(notification_api):
    from sqlalchemy import select
    from app.db import utcnow
    from app.models import NotificationIntent, OutboxJob
    from app.worker import process_notifications
    client, factory, settings = notification_api
    settings.line_channel_access_token = "fake-test-only"
    ids = _seed_notification(factory)
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("simulated", request=request)
        return httpx.Response(409, headers={"x-line-accepted-request-id": "accepted-earlier"})

    transport = httpx.MockTransport(handler)
    assert process_notifications(factory, settings, transport=transport) == 2
    with factory() as db:
        intent = db.get(NotificationIntent, ids["LINE"])
        assert intent.status == "RETRY_WAIT"
        original_key = intent.retry_key
        assert "line-subject" not in json.dumps(intent.payload_snapshot)
        job = db.scalar(select(OutboxJob).where(OutboxJob.intent_id == intent.id))
        job.available_at = utcnow()
        db.commit()
    assert process_notifications(factory, settings, transport=transport) == 1
    with factory() as db:
        intent = db.get(NotificationIntent, ids["LINE"])
        assert intent.status == "API_ACCEPTED"
        assert intent.retry_key == original_key
    assert requests[0].content == requests[1].content
    assert requests[0].headers["x-line-retry-key"] == requests[1].headers["x-line-retry-key"]
    assert process_notifications(factory, settings, transport=transport) == 0
    assert len(client.get("/api/v1/notifications").json()["data"]["items"]) == 1


def test_unknown_older_than_24_hours_never_sends_with_new_key(notification_api):
    from sqlalchemy import select
    from app.db import utcnow
    from app.models import NotificationIntent, OutboxJob
    from app.worker import process_notifications
    _, factory, settings = notification_api
    settings.line_channel_access_token = "fake-test-only"
    ids = _seed_notification(factory)
    calls = []
    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("simulated", request=request)
    transport = httpx.MockTransport(timeout)
    process_notifications(factory, settings, transport=transport)
    with factory() as db:
        row = db.get(NotificationIntent, ids["LINE"])
        original_key = row.retry_key
        row.first_attempt_at = utcnow() - timedelta(hours=25)
        job = db.scalar(select(OutboxJob).where(OutboxJob.intent_id == row.id))
        job.available_at = utcnow()
        db.commit()
    process_notifications(factory, settings, transport=transport)
    with factory() as db:
        row = db.get(NotificationIntent, ids["LINE"])
        assert row.status == "UNKNOWN" and row.retry_key == original_key
    assert len(calls) == 1


def test_unconfigured_line_and_stale_reminders_are_not_sent(notification_api):
    from app.models import NotificationIntent, Task
    from app.worker import process_notifications
    _, factory, settings = notification_api
    ids = _seed_notification(factory, purpose="TASK_CREATED", task=True)
    with factory() as db:
        db.get(Task, "task").status = "SUBMITTED"
        db.commit()
    process_notifications(factory, settings)
    with factory() as db:
        for intent_id in ids.values():
            assert db.get(NotificationIntent, intent_id).status == "SKIPPED"


def test_worker_does_not_fake_line_acceptance_when_unconfigured(notification_api):
    from app.models import NotificationIntent
    from app.worker import process_notifications
    _, factory, settings = notification_api
    ids = _seed_notification(factory)
    assert process_notifications(factory, settings) == 2
    with factory() as db:
        assert db.get(NotificationIntent, ids["IN_APP"]).status == "API_ACCEPTED"
        line = db.get(NotificationIntent, ids["LINE"])
        assert line.status == "DEAD_LETTER"
        assert line.last_error_code == "LINE_NOT_CONFIGURED"
        assert line.first_attempt_at is None and line.retry_key is None


def test_notification_read_scope_and_binding_revocation(notification_api):
    from app.db import utcnow
    from app.models import IdentityLink, NotificationIntent
    from app.worker import process_notifications
    client, factory, settings = notification_api
    ids = _seed_notification(factory)
    with factory() as db:
        db.get(IdentityLink, "line-link").revoked_at = utcnow()
        db.commit()
    process_notifications(factory, settings)
    with factory() as db:
        assert db.get(NotificationIntent, ids["LINE"]).status == "SKIPPED"
    assert client.get("/api/v1/notifications", headers={"x-test-actor": "intruder"}).json()["data"]["items"] == []
    assert client.post(f"/api/v1/notifications/{ids['IN_APP']}/view", headers={"x-test-actor": "intruder"}).status_code == 404
    assert client.post(f"/api/v1/notifications/{ids['IN_APP']}/view").status_code == 204


def test_worker_reclaims_expired_lease_with_original_frozen_payload(notification_api):
    from sqlalchemy import select
    from app.db import utcnow
    from app.models import NotificationAttempt, NotificationIntent, OutboxJob
    from app.notification_delivery import encrypt_snapshot, line_payload
    from app.worker import process_notifications
    _, factory, settings = notification_api
    settings.line_channel_access_token = "fake-test-only"
    ids = _seed_notification(factory)
    with factory() as db:
        intent = db.get(NotificationIntent, ids["LINE"])
        intent.payload_snapshot = encrypt_snapshot(line_payload(intent, "line-subject", settings), settings.secret_key)
        intent.retry_key, intent.first_attempt_at = "frozen-retry-key", utcnow() - timedelta(minutes=1)
        intent.status = "PROCESSING"
        job = db.scalar(select(OutboxJob).where(OutboxJob.intent_id == intent.id))
        job.lease_owner, job.lease_until = "dead-process", utcnow() - timedelta(seconds=1)
        job.attempt_count = 1
        db.add(NotificationAttempt(intent_id=intent.id, attempt_no=1, retry_key=intent.retry_key,
                                   payload_snapshot=intent.payload_snapshot, result="IN_FLIGHT"))
        db.commit()
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(409, headers={"x-line-accepted-request-id": "before-process-death"})
    process_notifications(factory, settings, transport=httpx.MockTransport(handler))
    assert len(requests) == 1
    assert requests[0].headers["x-line-retry-key"] == "frozen-retry-key"
    with factory() as db:
        assert db.get(NotificationIntent, ids["LINE"]).status == "API_ACCEPTED"
        statuses = db.scalars(select(NotificationAttempt.result).where(NotificationAttempt.intent_id == ids["LINE"]).order_by(NotificationAttempt.attempt_no)).all()
        assert statuses == ["UNKNOWN", "API_ACCEPTED"]


def _webhook(client, settings, events, destination="bot-id", corrupt=False):
    body = json.dumps({"destination": destination, "events": events}, separators=(",", ":")).encode()
    signature = base64.b64encode(hmac.new(settings.line_channel_secret.encode(), body, hashlib.sha256).digest()).decode()
    return client.post("/api/v1/webhooks/line", content=body + (b" " if corrupt else b""),
                       headers={"x-line-signature": signature, "content-type": "application/json"})


def test_webhook_tamper_destination_duplicates_and_out_of_order(notification_api):
    from sqlalchemy import func, select
    from app.db import utcnow
    from app.models import IdentityLink, WebhookInbox
    from app.worker import process_inbox
    client, factory, settings = notification_api
    with factory() as db:
        db.add(IdentityLink(id="link", account_id="owner", provider_id="provider", line_subject="subject"))
        db.commit()
    now_ms = int(utcnow().timestamp() * 1000)
    newer = {"webhookEventId": "new-unfollow", "type": "unfollow", "timestamp": now_ms,
             "source": {"type": "user", "userId": "subject"}}
    assert _webhook(client, settings, [newer], corrupt=True).status_code == 401
    assert _webhook(client, settings, [newer], destination="wrong-bot").status_code == 400
    first = _webhook(client, settings, [newer])
    assert first.status_code == 200 and first.json()["data"]["new_events"] == 1
    assert _webhook(client, settings, [newer]).json()["data"]["new_events"] == 0
    assert process_inbox(factory, settings) == 1
    older = {**newer, "webhookEventId": "old-follow", "type": "follow", "timestamp": now_ms - 60_000}
    assert _webhook(client, settings, [older]).status_code == 200
    assert process_inbox(factory, settings) == 1
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(WebhookInbox)) == 2
        assert db.get(IdentityLink, "link").observed_follow_status == "UNFOLLOWED"
        assert db.scalar(select(WebhookInbox).where(WebhookInbox.event_id == "old-follow")).status == "SKIPPED_OLDER"
