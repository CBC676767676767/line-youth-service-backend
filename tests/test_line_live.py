"""Real reply adapter contract exercised exclusively with synthetic MockTransport."""

import importlib.util
import json
import logging
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.db import make_engine, utcnow
from app.line_replies import FakeLineReplySender, LiveLineReplySender, process_line_replies
from app.line_reply_models import LineReplyJob
from test_line_replies import event, jobs, stored_data, webhook
from test_line_replies import reply_api as reply_api


ACCESS_TOKEN = "synthetic-channel-access-token"
REPLY_TOKEN = "synthetic-reply-token-private"
MESSAGES = [{"type": "text", "text": "服務選單"}]


@pytest.fixture
def live_api(reply_api):
    client, factory, settings, buffer = reply_api
    settings.line_reply_mode = "live"
    settings.line_channel_access_token = ACCESS_TOKEN
    return client, factory, settings, buffer


def sender_for(handler):
    return LiveLineReplySender(ACCESS_TOKEN, transport=httpx.MockTransport(handler))


def test_signed_inbound_token_is_ephemeral_and_accepted_once(live_api, caplog):
    client, factory, settings, buffer = live_api
    incoming = event(replyToken=REPLY_TOKEN)
    assert webhook(client, settings, [incoming]).status_code == 200
    assert len(buffer.pending_ids()) == 1
    assert next(iter(buffer._items.values())).reply_token == REPLY_TOKEN
    assert REPLY_TOKEN not in repr(buffer._items)
    seen = []

    def accepted(request):
        assert request.method == "POST"
        assert str(request.url) == "https://api.line.me/v2/bot/message/reply"
        assert request.headers["Authorization"] == "Bearer " + ACCESS_TOKEN
        payload = json.loads(request.content)
        assert set(payload) == {"replyToken", "messages"}
        assert payload["replyToken"] == REPLY_TOKEN
        assert "private-line-subject" not in request.content.decode()
        assert not buffer.pending_ids() and not buffer._items
        assert jobs(factory)[0].status == "SENDING"
        assert jobs(factory)[0].attempt_count == 1
        assert all(value <= 8 for value in request.extensions["timeout"].values())
        seen.append(request.url.path)
        return httpx.Response(200, json={"sentMessages": [{"id": "private-response-body"}]})

    sender = sender_for(accepted)
    with caplog.at_level(logging.DEBUG):
        counts = process_line_replies(factory, settings, buffer=buffer, sender=sender)
    assert counts["api_accepted"] == 1 and counts["fake_sent"] == 0
    assert webhook(client, settings, [incoming]).json()["data"]["new_events"] == 0
    assert process_line_replies(factory, settings, buffer=buffer, sender=sender)["processed"] == 0
    assert seen == ["/v2/bot/message/reply"]
    receipt = jobs(factory)[0]
    assert receipt.status == "API_ACCEPTED" and receipt.http_status == 200
    assert receipt.delivery_mode == "live" and receipt.error_code is None
    for secret in (REPLY_TOKEN, ACCESS_TOKEN, "private-line-subject", "private-response-body"):
        assert secret not in stored_data(factory) and secret not in caplog.text


@pytest.mark.parametrize("status,expected", [
    (200, "API_ACCEPTED"), (202, "FAILED"), (302, "FAILED"), (307, "FAILED"),
    (400, "FAILED"), (401, "FAILED"), (403, "FAILED"), (429, "FAILED"),
    (500, "UNKNOWN"), (503, "UNKNOWN"),
])
def test_http_outcomes_never_retry_follow_redirects_or_push(live_api, status, expected):
    client, factory, settings, buffer = live_api
    assert webhook(client, settings, [event(replyToken=REPLY_TOKEN)]).status_code == 200
    requests = []

    def respond(request):
        requests.append(request.url.path)
        return httpx.Response(status, headers={"Location": "https://untrusted.example/collect"},
                              text=REPLY_TOKEN + ACCESS_TOKEN)

    sender = sender_for(respond)
    counts = process_line_replies(factory, settings, buffer=buffer, sender=sender)
    assert counts[expected.lower()] == 1
    assert jobs(factory)[0].status == expected and jobs(factory)[0].http_status == status
    assert process_line_replies(factory, settings, buffer=buffer, sender=sender)["processed"] == 0
    assert requests == ["/v2/bot/message/reply"]
    assert REPLY_TOKEN not in stored_data(factory) and ACCESS_TOKEN not in stored_data(factory)


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError])
def test_network_uncertainty_is_terminal_without_secret_logs(live_api, error_type, caplog):
    client, factory, settings, buffer = live_api
    assert webhook(client, settings, [event(replyToken=REPLY_TOKEN)]).status_code == 200
    seen = []

    def uncertain(request):
        seen.append(request.url.path)
        raise error_type(REPLY_TOKEN + ACCESS_TOKEN, request=request)

    sender = sender_for(uncertain)
    assert process_line_replies(factory, settings, buffer=buffer, sender=sender)["unknown"] == 1
    assert process_line_replies(factory, settings, buffer=buffer, sender=sender)["processed"] == 0
    receipt = jobs(factory)[0]
    assert receipt.http_status is None and receipt.error_code == "LINE_REPLY_NETWORK_UNKNOWN"
    assert receipt.attempt_count == 1 and len(seen) == 1
    assert REPLY_TOKEN not in caplog.text and ACCESS_TOKEN not in caplog.text


@pytest.mark.parametrize("enqueued,active,sender_mode", [
    ("fake", "live", "live"), ("live", "fake", "fake"),
    ("live", "live", "fake"), ("fake", "fake", "live"),
])
def test_delivery_mode_changes_cannot_convert_or_reroute_queued_reply(reply_api, enqueued, active, sender_mode):
    client, factory, settings, buffer = reply_api
    settings.line_reply_mode = enqueued
    assert webhook(client, settings, [event(replyToken=REPLY_TOKEN)]).status_code == 200
    settings.line_reply_mode = active
    seen = []
    sender = sender_for(lambda request: seen.append(request) or httpx.Response(200)) \
        if sender_mode == "live" else FakeLineReplySender()
    result = process_line_replies(factory, settings, buffer=buffer, sender=sender)
    assert result["failed"] == 1 and jobs(factory)[0].error_code == "LINE_REPLY_MODE_MISMATCH"
    assert not buffer.pending_ids() and not seen
    if isinstance(sender, FakeLineReplySender):
        assert not sender.take_replies()


def test_live_dispatch_requires_explicit_live_sender(live_api):
    client, factory, settings, buffer = live_api
    assert webhook(client, settings, [event(replyToken=REPLY_TOKEN)]).status_code == 200
    assert process_line_replies(factory, settings, buffer=buffer)["failed"] == 1
    assert jobs(factory)[0].error_code == "LINE_REPLY_MODE_MISMATCH"


def test_invalid_signature_cannot_reach_live_sender(live_api):
    client, factory, settings, buffer = live_api
    assert webhook(client, settings, [event(replyToken=REPLY_TOKEN)], corrupt=True).status_code == 401
    seen = []
    sender = sender_for(lambda request: seen.append(request) or httpx.Response(200))
    assert process_line_replies(factory, settings, buffer=buffer, sender=sender)["processed"] == 0
    assert not seen and not jobs(factory)


def test_expired_or_cleared_live_tokens_never_reach_transport(live_api):
    client, factory, settings, buffer = live_api
    old = event("expired-live", replyToken=REPLY_TOKEN,
                timestamp=int((utcnow() - timedelta(seconds=60)).timestamp() * 1000))
    assert webhook(client, settings, [old]).status_code == 200
    assert jobs(factory)[0].status == "EXPIRED" and not buffer.pending_ids()
    assert webhook(client, settings, [event("cleared-live", replyToken=REPLY_TOKEN)]).status_code == 200
    assert buffer.pending_ids()
    buffer.clear()
    seen = []
    sender = sender_for(lambda request: seen.append(request) or httpx.Response(200))
    assert process_line_replies(factory, settings, buffer=buffer, sender=sender)["processed"] == 0
    assert not seen and not buffer._items
    with factory() as db:
        row = db.scalar(select(LineReplyJob).where(LineReplyJob.event_id == "cleared-live"))
        row.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert process_line_replies(factory, settings, buffer=buffer, sender=sender)["expired"] == 1
    assert not seen and REPLY_TOKEN not in stored_data(factory)


def test_live_render_failure_uses_one_generic_reply_and_records_failed(live_api, monkeypatch):
    client, factory, settings, buffer = live_api
    assert webhook(client, settings, [event(replyToken=REPLY_TOKEN)]).status_code == 200

    def failed(*_args, **_kwargs):
        raise ValueError("private-renderer-error")

    monkeypatch.setattr("app.line_replies._render_reply", failed)
    seen = []

    def accept(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200)

    counts = process_line_replies(factory, settings, buffer=buffer, sender=sender_for(accept))
    assert counts["failed"] == 1 and counts["api_accepted"] == 0
    assert jobs(factory)[0].error_code == "LINE_REPLY_BUILD_FAILED" and jobs(factory)[0].http_status == 200
    assert len(seen) == 1 and "暫時無法完成" in json.dumps(seen[0], ensure_ascii=False)
    assert "private-renderer-error" not in json.dumps(seen)


def test_live_response_commit_failure_cannot_resend(live_api, monkeypatch):
    client, factory, settings, buffer = live_api
    assert webhook(client, settings, [event(replyToken=REPLY_TOKEN)]).status_code == 200
    seen = []
    sender = sender_for(lambda request: seen.append(request.url.path) or httpx.Response(200))
    session_class, original_commit = factory.class_, factory.class_.commit
    commits = 0

    def fail_receipt(db):
        nonlocal commits
        commits += 1
        if commits == 3:
            raise OperationalError("commit", {}, Exception("synthetic write failure"))
        return original_commit(db)

    monkeypatch.setattr(session_class, "commit", fail_receipt)
    with pytest.raises(OperationalError):
        process_line_replies(factory, settings, buffer=buffer, sender=sender)
    monkeypatch.setattr(session_class, "commit", original_commit)
    assert jobs(factory)[0].status == "SENDING" and not buffer.pending_ids()
    assert process_line_replies(factory, settings, buffer=buffer, sender=sender)["processed"] == 0
    with factory() as db:
        db.scalar(select(LineReplyJob)).expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert process_line_replies(factory, settings, buffer=buffer, sender=sender)["unknown"] == 1
    assert seen == ["/v2/bot/message/reply"]


def test_live_http_client_disables_proxy_and_redirects(monkeypatch):
    original = httpx.Client
    options = []

    def observed(**kwargs):
        options.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(httpx, "Client", observed)
    result = sender_for(lambda _request: httpx.Response(200)).send(reply_token=REPLY_TOKEN, messages=MESSAGES)
    assert result.status == "API_ACCEPTED"
    assert len(options) == 1 and options[0]["trust_env"] is False
    assert options[0]["follow_redirects"] is False and options[0]["timeout"] <= 8


def test_invalid_reply_token_fails_without_transport():
    seen = []
    sender = sender_for(lambda request: seen.append(request) or httpx.Response(200))
    assert sender.send(reply_token="invalid\nheader", messages=MESSAGES).status == "FAILED"
    assert sender.send(reply_token="0" * 32, messages=MESSAGES).status == "FAILED"
    assert not seen


def migration(name):
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / name
    spec = importlib.util.spec_from_file_location("reply_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_live_migration_preserves_old_receipts_and_truthful_downgrade(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    old = migration("d76e14b290a1_line_reply_receipts.py")
    current = migration("e31b7a620d42_live_line_reply_receipts.py")
    with engine.begin() as connection:
        operation = Operations(MigrationContext.configure(connection))
        old.op = operation
        current.op = operation
        old.upgrade()
        insert = text("""INSERT INTO line_reply_jobs
            (id, channel_id, event_id, event_type, received_at, expires_at, delivery_mode,
             status, attempt_count, version, created_at, updated_at)
            VALUES (:id, 'test', :id, 'message', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                    :mode, :status, 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""")
        connection.execute(insert, {"id": "old-fake", "mode": "fake", "status": "FAKE_SENT"})
        current.upgrade()
        assert connection.scalar(text("SELECT status FROM line_reply_jobs WHERE id = 'old-fake'")) == "FAKE_SENT"
        current.downgrade()
        current.upgrade()
        connection.execute(insert, {"id": "new-live", "mode": "live", "status": "API_ACCEPTED"})
        with pytest.raises(RuntimeError, match="live LINE reply receipts"):
            current.downgrade()
        assert connection.scalar(text("SELECT status FROM line_reply_jobs WHERE id = 'new-live'")) == "API_ACCEPTED"
        columns = {column["name"] for column in inspect(connection).get_columns("line_reply_jobs")}
        assert not columns.intersection({"reply_token", "actor_key", "messages", "user_id", "inputs"})
        with pytest.raises(IntegrityError):
            connection.execute(insert, {"id": "invalid", "mode": "push", "status": "API_ACCEPTED"})
    engine.dispose()
