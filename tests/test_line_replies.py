"""Local fake-sender tests for signatures, privacy, deduplication and interruption."""

import base64
import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.db import utcnow
from app.line_replies import FakeLineReplySender, LineReplyBuffer, line_reply_actor_key, process_line_replies
from app.line_reply_models import LineReplyJob
from app.models import IdentityLink, WebhookInbox


@pytest.fixture
def reply_api(tmp_path):
    from delivery_support import delivery_environment
    client, factory, base_settings, engine = delivery_environment(tmp_path)
    config = base_settings.model_dump()
    config.update(line_bot_enabled=True, line_reply_mode="fake", line_public_precheck_url="",
                  line_channel_access_token="must-never-be-used")
    settings = SimpleNamespace(**config)
    buffer = LineReplyBuffer()
    client.app.state.settings = settings
    client.app.state.line_reply_buffer = buffer
    with client:
        yield client, factory, settings, buffer
    buffer.clear()
    engine.dispose()


def event(event_id="reply-event", **changes):
    result = {
        "webhookEventId": event_id, "type": "message", "mode": "active",
        "timestamp": int(utcnow().timestamp() * 1000), "replyToken": "mock-reply-token",
        "source": {"type": "user", "userId": "private-line-subject"},
        "message": {"type": "text", "id": "unused-message-id", "text": "選單"},
    }
    result.update(changes)
    return result


def webhook(client, settings, events, *, destination="bot-id", corrupt=False):
    raw = json.dumps({"destination": destination, "events": events}, ensure_ascii=False).encode()
    signature = base64.b64encode(hmac.new(settings.line_channel_secret.encode(), raw, hashlib.sha256).digest())
    return client.post("/api/v1/webhooks/line", content=raw + (b" " if corrupt else b""),
                       headers={"x-line-signature": signature.decode(), "content-type": "application/json"})


def jobs(factory):
    with factory() as db:
        return list(db.scalars(select(LineReplyJob).order_by(LineReplyJob.event_id)))


def stored_data(factory):
    with factory() as db:
        values = []
        for model in (WebhookInbox, LineReplyJob):
            for row in db.scalars(select(model)):
                values.append({column.name: getattr(row, column.name) for column in model.__table__.columns})
        return json.dumps(values, default=str, ensure_ascii=False)


def test_only_signed_matching_destination_can_enqueue(reply_api):
    client, factory, settings, buffer = reply_api
    assert webhook(client, settings, [event()], corrupt=True).status_code == 401
    assert webhook(client, settings, [event()], destination="other-bot").status_code == 400
    assert jobs(factory) == [] and buffer.pending_ids() == []
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(WebhookInbox)) == 0


def test_invalid_batch_never_partially_enqueues_valid_event(reply_api):
    client, factory, settings, buffer = reply_api
    malformed = event("invalid", timestamp="private free-text timestamp")
    assert webhook(client, settings, [event(), malformed]).status_code == 400
    assert jobs(factory) == [] and buffer.pending_ids() == [] and stored_data(factory) == "[]"


def test_unknown_input_never_persisted_or_echoed(reply_api, monkeypatch):
    client, factory, settings, buffer = reply_api
    private = "王測試 A123456789 0912345678 bank-account-private"
    incoming = event(message={"type": "text", "id": "private-msg-id", "text": private})
    assert webhook(client, settings, [incoming]).status_code == 200
    persisted = stored_data(factory)
    for sensitive in (private, "private-line-subject", "mock-reply-token", "private-msg-id", "隱私提醒"):
        assert sensitive not in persisted
    commands = []

    def render(envelope, _settings, *, store, conversation_now=None):
        commands.append(envelope.command)
        return [{"type": "text", "text": "請勿傳送個人資料。"}]

    monkeypatch.setattr("app.line_replies._render_reply", render)
    sender = FakeLineReplySender()
    result = process_line_replies(factory, settings, buffer=buffer, sender=sender)
    assert result["fake_sent"] == 1 and commands == ["隱私提醒"]
    replies = sender.take_replies()
    assert len(replies) == 1 and private not in json.dumps(replies)
    assert buffer.pending_ids() == []


def test_duplicate_event_single_claim_and_fake_reply_without_network(reply_api, monkeypatch):
    client, factory, settings, buffer = reply_api
    incoming = event()
    assert webhook(client, settings, [incoming, incoming]).json()["data"]["new_events"] == 1
    assert webhook(client, settings, [incoming]).json()["data"]["new_events"] == 0
    assert len(jobs(factory)) == len(buffer.pending_ids()) == 1

    def forbidden(*_args, **_kwargs):
        raise AssertionError("No network adapter may be used by the local fake sender")

    monkeypatch.setattr(httpx.Client, "send", forbidden)

    class Observer(FakeLineReplySender):
        def send(self, *, actor_key, messages):
            assert buffer.pending_ids() == []
            row = jobs(factory)[0]
            assert row.status == "SENDING" and row.attempt_count == 1
            super().send(actor_key=actor_key, messages=messages)

    sender = Observer()
    assert process_line_replies(factory, settings, buffer=buffer, sender=sender)["fake_sent"] == 1
    assert process_line_replies(factory, settings, buffer=buffer, sender=sender)["processed"] == 0
    row = jobs(factory)[0]
    assert row.status == "FAKE_SENT" and row.http_status is None and row.attempt_count == 1
    replies = sender.take_replies()
    assert len(replies) == 1 and 1 <= len(replies[0]["messages"]) <= 5
    assert "mock-reply-token" not in json.dumps(replies) and "must-never-be-used" not in json.dumps(replies)
    assert sender.take_replies() == []


@pytest.mark.parametrize("source", [{"type": "group", "userId": "private-line-subject", "groupId": "g"},
                                    {"type": "room", "userId": "private-line-subject", "roomId": "r"},
                                    {"type": "user"}])
def test_unsupported_sources_never_reply(reply_api, source):
    client, factory, settings, buffer = reply_api
    assert webhook(client, settings, [event(source=source)]).status_code == 200
    assert jobs(factory) == [] and buffer.pending_ids() == []
    assert "private-line-subject" not in stored_data(factory)


@pytest.mark.parametrize("changes", [{"mode": "standby"}, {"replyToken": ""}, {"replyToken": "0" * 32},
                                      {"replyToken": "contains\nnewlines"}, {"type": "unsend"}])
def test_no_reply_for_standby_unsupported_or_missing_token(reply_api, changes):
    client, factory, settings, buffer = reply_api
    assert webhook(client, settings, [event(**changes)]).status_code == 200
    assert jobs(factory) == [] and buffer.pending_ids() == []


@pytest.mark.parametrize("message", [{"type": "image", "id": "private-file"}, {"type": "file"},
                                     {"type": "location", "latitude": 1, "longitude": 1}])
def test_attachments_receive_generic_privacy_guidance(reply_api, message):
    client, factory, settings, buffer = reply_api
    assert webhook(client, settings, [event(message=message)]).status_code == 200
    sender = FakeLineReplySender()
    assert process_line_replies(factory, settings, buffer=buffer, sender=sender)["fake_sent"] == 1
    rendered = json.dumps(sender.take_replies(), ensure_ascii=False)
    assert "聊天室不收" in rendered and "附件" in rendered and "private-file" not in rendered
    assert "latitude" not in stored_data(factory)


def test_stale_event_expires_and_future_event_cannot_extend_budget(reply_api):
    client, factory, settings, buffer = reply_api
    old = event("old", timestamp=int((utcnow() - timedelta(minutes=1)).timestamp() * 1000))
    future = event("future", timestamp=int((utcnow() + timedelta(days=1)).timestamp() * 1000))
    assert webhook(client, settings, [old, future]).status_code == 200
    rows = {row.event_id: row for row in jobs(factory)}
    assert rows["old"].status == "EXPIRED" and rows["old"].attempt_count == 0
    assert rows["future"].expires_at - rows["future"].received_at <= timedelta(seconds=50)
    assert len(buffer.pending_ids()) == 1


def test_disabling_replies_cancels_pending(reply_api):
    client, factory, settings, buffer = reply_api
    assert webhook(client, settings, [event()]).status_code == 200
    settings.line_bot_enabled = False
    sender = FakeLineReplySender()
    assert process_line_replies(factory, settings, buffer=buffer, sender=sender)["failed"] == 1
    assert jobs(factory)[0].error_code == "LINE_REPLIES_DISABLED"
    assert sender.take_replies() == [] and buffer.pending_ids() == []


@pytest.mark.parametrize("mode", ["disabled", "dry_run", "invalid"])
def test_unsupported_modes_never_enqueue(reply_api, mode):
    client, factory, settings, buffer = reply_api
    settings.line_reply_mode = mode
    assert webhook(client, settings, [event()]).status_code == 200
    assert jobs(factory) == [] and buffer.pending_ids() == []


def test_fake_sender_failure_is_not_replayed_or_logged(reply_api, caplog):
    client, factory, settings, buffer = reply_api
    assert webhook(client, settings, [event()]).status_code == 200

    class FailingSender(FakeLineReplySender):
        def send(self, *, actor_key, messages):
            super().send(actor_key=actor_key, messages=messages)
            raise RuntimeError("mock-reply-token private-line-subject must-never-be-used")

    sender = FailingSender()
    assert process_line_replies(factory, settings, buffer=buffer, sender=sender)["unknown"] == 1
    assert process_line_replies(factory, settings, buffer=buffer, sender=sender)["processed"] == 0
    assert len(sender.take_replies()) == 1 and jobs(factory)[0].status == "UNKNOWN"
    for value in ("mock-reply-token", "private-line-subject", "must-never-be-used"):
        assert value not in caplog.text and value not in stored_data(factory)


@pytest.mark.parametrize(("before", "after"), [("PENDING", "EXPIRED"), ("SENDING", "UNKNOWN")])
def test_process_loss_never_replays_lost_memory(reply_api, before, after):
    client, factory, settings, buffer = reply_api
    assert webhook(client, settings, [event()]).status_code == 200
    buffer.clear()
    with factory() as db:
        row = db.scalar(select(LineReplyJob))
        row.status = before
        row.attempt_count = 1 if before == "SENDING" else 0
        row.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    sender = FakeLineReplySender()
    result = process_line_replies(factory, settings, buffer=buffer, sender=sender)
    assert result[after.lower()] == 1 and sender.take_replies() == [] and jobs(factory)[0].status == after


def test_follow_unfollow_processing_and_order_are_preserved(reply_api):
    from app.worker import process_inbox
    client, factory, settings, buffer = reply_api
    with factory() as db:
        db.add(IdentityLink(id="line-follow-link", account_id="owner", provider_id="provider",
                            line_subject="private-line-subject"))
        db.commit()
    now = int(utcnow().timestamp() * 1000)
    assert webhook(client, settings, [event("newer-unfollow", type="unfollow", timestamp=now)]).status_code == 200
    assert process_inbox(factory, settings) == 1
    assert webhook(client, settings, [event("older-follow", type="follow", timestamp=now - 1000)]).status_code == 200
    assert process_inbox(factory, settings) == 1
    with factory() as db:
        assert db.get(IdentityLink, "line-follow-link").observed_follow_status == "UNFOLLOWED"
        assert db.scalar(select(WebhookInbox).where(WebhookInbox.event_id == "older-follow")).status == "SKIPPED_OLDER"
    assert len(jobs(factory)) == len(buffer.pending_ids()) == 1


def test_buffer_capacity_fails_closed_without_secret_persistence(reply_api):
    client, factory, settings, _ = reply_api
    buffer = LineReplyBuffer(max_items=1)
    client.app.state.line_reply_buffer = buffer
    assert webhook(client, settings, [event("one"), event("two")]).status_code == 200
    assert len(buffer.pending_ids()) == 1
    rows = {row.event_id: row for row in jobs(factory)}
    assert rows["one"].status == "PENDING" and rows["two"].error_code == "LINE_REPLY_BUFFER_FULL"
    assert "mock-reply-token" not in stored_data(factory)
    buffer.clear()


def test_transaction_failure_removes_new_memory_and_receipts(reply_api, monkeypatch):
    client, factory, settings, buffer = reply_api
    session_class = factory.class_
    original_commit = session_class.commit

    def failed_commit(_db):
        raise OperationalError("commit", {}, Exception("simulated unavailable storage"))

    monkeypatch.setattr(session_class, "commit", failed_commit)
    response = webhook(client, settings, [event()])
    monkeypatch.setattr(session_class, "commit", original_commit)
    assert response.status_code == 503 and buffer.pending_ids() == [] and jobs(factory) == []
    assert stored_data(factory) == "[]"


def test_precheck_configuration_failure_returns_generic_failure_notice(reply_api, monkeypatch):
    client, factory, settings, buffer = reply_api
    assert webhook(client, settings, [event()]).status_code == 200

    def failed_build(*_args, **_kwargs):
        raise ValueError("private-line-subject must-never-be-used")

    monkeypatch.setattr("app.line_replies._render_reply", failed_build)
    sender = FakeLineReplySender()
    assert process_line_replies(factory, settings, buffer=buffer, sender=sender)["failed"] == 1
    assert jobs(factory)[0].error_code == "LINE_REPLY_BUILD_FAILED"
    rendered = json.dumps(sender.take_replies(), ensure_ascii=False)
    assert "暫時無法完成" in rendered and "未代為送件" in rendered
    assert "private-line-subject" not in rendered and "must-never-be-used" not in rendered
    assert process_line_replies(factory, settings, buffer=buffer, sender=sender)["processed"] == 0
    assert "private-line-subject" not in stored_data(factory)


def test_two_workers_cannot_dispatch_same_reply(reply_api):
    client, factory, settings, buffer = reply_api
    assert webhook(client, settings, [event()]).status_code == 200
    start = Barrier(2)
    sender = FakeLineReplySender()

    def worker():
        start.wait(timeout=5)
        return process_line_replies(factory, settings, buffer=buffer, sender=sender)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))
    assert sum(result["fake_sent"] for result in results) == 1
    assert len(sender.take_replies()) == 1 and jobs(factory)[0].attempt_count == 1


def test_same_batch_commands_keep_input_order_instead_of_random_id_order(reply_api, monkeypatch):
    client, factory, settings, buffer = reply_api
    ids = iter(["z-job", "a-job", "m-job"])
    monkeypatch.setattr("app.line_replies.new_id", lambda: next(ids))
    commands = ["安全", "協助", "文件"]
    incoming = [event(str(index), message={"type": "text", "text": command})
                for index, command in enumerate(commands)]
    assert webhook(client, settings, incoming).status_code == 200
    observed = []

    def render(envelope, _settings, *, store, conversation_now=None):
        observed.append(envelope.command)
        return [{"type": "text", "text": envelope.command}]

    monkeypatch.setattr("app.line_replies._render_reply", render)
    assert process_line_replies(factory, settings, buffer=buffer)["fake_sent"] == 3
    assert observed == commands


def test_final_commit_failure_after_fake_acceptance_never_resends(reply_api, monkeypatch):
    client, factory, settings, buffer = reply_api
    assert webhook(client, settings, [event()]).status_code == 200
    session_class = factory.class_
    original_commit = session_class.commit
    commits = 0

    def fail_final_commit(db):
        nonlocal commits
        commits += 1
        if commits == 3:
            raise OperationalError("commit", {}, Exception("simulated final-write outage"))
        return original_commit(db)

    monkeypatch.setattr(session_class, "commit", fail_final_commit)
    sender = FakeLineReplySender()
    with pytest.raises(OperationalError):
        process_line_replies(factory, settings, buffer=buffer, sender=sender)
    monkeypatch.setattr(session_class, "commit", original_commit)
    assert len(sender.take_replies()) == 1 and jobs(factory)[0].status == "SENDING"
    assert process_line_replies(factory, settings, buffer=buffer, sender=sender)["processed"] == 0
    assert sender.take_replies() == []
    with factory() as db:
        db.scalar(select(LineReplyJob)).expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert process_line_replies(factory, settings, buffer=buffer, sender=sender)["unknown"] == 1
    assert jobs(factory)[0].status == "UNKNOWN" and sender.take_replies() == []


def test_simulator_drain_can_be_scoped_to_one_actor(reply_api):
    client, factory, settings, buffer = reply_api
    other = event("other-event", source={"type": "user", "userId": "other-private-subject"})
    assert webhook(client, settings, [event(), other]).status_code == 200
    actor = line_reply_actor_key(settings, "private-line-subject")
    sender = FakeLineReplySender()
    result = process_line_replies(factory, settings, buffer=buffer, sender=sender, actor_key=actor)
    assert result["fake_sent"] == 1 and len(buffer.pending_ids()) == 1
    replies = sender.take_replies(actor)
    assert len(replies) == 1 and replies[0]["actor_key"] == actor
    assert sender.take_replies() == []
    rows = {row.event_id: row for row in jobs(factory)}
    assert rows["other-event"].status == "PENDING"


def test_virtual_conversation_clock_does_not_expire_delivery_queue(reply_api, monkeypatch):
    client, factory, settings, buffer = reply_api
    virtual_now = utcnow() + timedelta(minutes=25)
    assert webhook(client, settings, [event()]).status_code == 200
    observed = []

    def render(envelope, _settings, *, store, conversation_now=None):
        observed.append(conversation_now)
        return [{"type": "text", "text": "模擬時間已更新"}]

    monkeypatch.setattr("app.line_replies._render_reply", render)
    sender = FakeLineReplySender()
    result = process_line_replies(factory, settings, buffer=buffer, sender=sender,
                                  conversation_now=virtual_now)
    assert result["fake_sent"] == 1 and observed == [virtual_now]
    row = jobs(factory)[0]
    assert row.completed_at < virtual_now - timedelta(minutes=24)
    assert row.expires_at - row.received_at <= timedelta(seconds=50)


def test_simulator_can_override_conversation_store_without_touching_shared_store(reply_api, monkeypatch):
    from app.line_conversation import ConversationStore

    client, factory, settings, buffer = reply_api
    assert webhook(client, settings, [event()]).status_code == 200
    isolated_store = ConversationStore()
    observed = []

    def render(envelope, _settings, *, store, conversation_now=None):
        observed.append(store)
        return [{"type": "text", "text": "工作階段已隔離"}]

    monkeypatch.setattr("app.line_replies._render_reply", render)
    result = process_line_replies(factory, settings, buffer=buffer, conversation_store=isolated_store)
    assert result["fake_sent"] == 1 and observed == [isolated_store]
    assert isolated_store is not buffer.conversations


@pytest.mark.parametrize("operation", ["result", "precheck", "safety", "safety_postback"])
def test_render_failure_invalidates_only_precheck_results(reply_api, monkeypatch, operation):
    from app import line_conversation as chat
    from app.precheck_engine import evaluate, load_bundle
    from app.precheck_schema import PrecheckInput
    from test_line_conversation import choose, handoff

    client, factory, settings, buffer = reply_api
    actor = line_reply_actor_key(settings, "private-line-subject")
    now = utcnow()
    isolated_store = chat.ConversationStore()
    bundle = load_bundle(settings.precheck_rules_path)

    def reply(command, **kwargs):
        return chat.build_reply(command, actor, bundle, settings.line_channel_secret, now,
                                 public_url="http://127.0.0.1:8765/precheck", store=isolated_store, **kwargs)

    messages = reply("預檢")
    for label in ("已經購買", "ChatGPT", "月費訂閱", "軟體官方網站"):
        messages = reply(choose(messages, label))
    token = handoff(messages)
    evaluated = evaluate(PrecheckInput(), bundle, now=now)
    assert chat.record_handoff_result(token, evaluated, store=isolated_store, now=now)
    original = chat.conversation_state(actor, store=isolated_store, now=now)["result"]
    reply("_result", event_key="cached-prior-result")
    if operation == "result":
        incoming = event(type="postback", postback={"data": "youth:result"})
    elif operation == "safety_postback":
        safety_command = choose(reply("安全"), "辨識可疑補助訊息")
        incoming = event(type="postback", postback={"data": safety_command})
    else:
        incoming = event(message={"type": "text", "text": "預檢" if operation == "precheck" else "安全"})
    assert webhook(client, settings, [incoming]).status_code == 200

    def failed_build(*_args, **_kwargs):
        raise ValueError("private error text must never survive")

    monkeypatch.setattr("app.line_replies._render_reply", failed_build)
    sender = FakeLineReplySender()
    result = process_line_replies(factory, settings, buffer=buffer, sender=sender,
                                  actor_key=actor, conversation_now=now,
                                  conversation_store=isolated_store)
    assert result["failed"] == 1 and len(sender.take_replies()) == 1
    current = chat.conversation_state(actor, store=isolated_store, now=now)["result"]
    if operation in {"result", "precheck"}:
        assert current == {"outcome": "failed"}
        rerendered = json.dumps(reply("_result", event_key="cached-prior-result"), ensure_ascii=False)
        assert "先前摘要已停止顯示" in rerendered
    else:
        assert current == original
    assert chat.conversation_state(actor, store=buffer.conversations, now=now)["status"] == "none"
    assert "private error text" not in stored_data(factory)
