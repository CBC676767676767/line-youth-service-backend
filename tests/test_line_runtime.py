"""Live runtime checks use local receipts and an in-process HTTP transport."""

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.db import make_engine, make_session_factory, utcnow
from app.line_replies import FakeLineReplySender, LiveLineReplySender, enqueue_line_reply
from app.line_reply_models import LineReplyJob
from app.line_runtime import LineRuntime


@pytest.fixture
def runtime_environment(tmp_path):
    settings = SimpleNamespace(
        line_bot_enabled=True, line_reply_mode="live",
        line_channel_access_token="runtime-test-access-token",
        line_messaging_channel_id="runtime-test-channel",
        line_channel_secret="runtime-test-channel-secret",
        secret_key="runtime-test-secret-" * 3,
        precheck_rules_path=Path(__file__).resolve().parents[1] / "app/data/precheck-hsinchu-115.json",
        precheck_official_application_url="", line_public_precheck_url="",
    )
    engine = make_engine(f"sqlite:///{tmp_path}/runtime.db")
    LineReplyJob.__table__.create(engine)
    yield settings, make_session_factory(engine)
    engine.dispose()


def enqueue(runtime, event_id="runtime-test-event"):
    now = utcnow()
    with runtime.factory() as db:
        job = enqueue_line_reply(db, {
            "webhookEventId": event_id, "type": "message", "mode": "active",
            "replyToken": "runtime-test-reply-token",
            "source": {"type": "user", "userId": "runtime-private-user"},
            "message": {"type": "text", "text": "選單"},
        }, runtime.settings, buffer=runtime.buffer, received_at=now, event_at=now)
        db.commit()
        return job.id


@pytest.mark.parametrize("enabled,mode", [(False, "live"), (True, "fake"), (True, "disabled")])
def test_non_live_runtime_fails_closed_without_credentials_or_database(enabled, mode):
    settings = SimpleNamespace(line_bot_enabled=enabled, line_reply_mode=mode)

    def forbidden_factory():
        raise AssertionError("Inactive runtime must not open the database")

    def forbidden_request(_request):
        raise AssertionError("Inactive runtime must not dispatch HTTP")

    runtime = LineRuntime(settings, forbidden_factory, transport=httpx.MockTransport(forbidden_request))
    assert not runtime.enabled and runtime.sender is None
    assert not any(runtime.tick().values())
    runtime.close()


def test_live_runtime_needs_valid_token_and_live_sender(runtime_environment):
    settings, factory = runtime_environment
    settings.line_channel_access_token = ""
    with pytest.raises(ValueError, match="access token"):
        LineRuntime(settings, factory)
    with pytest.raises(ValueError, match="live reply sender"):
        LineRuntime(settings, factory, sender=FakeLineReplySender())
    with pytest.raises(ValueError, match="either"):
        LineRuntime(settings, factory, sender=LiveLineReplySender("test-token"),
                    transport=httpx.MockTransport(lambda _request: httpx.Response(200)))


def test_live_tick_accepts_exactly_once_without_logging_secrets(runtime_environment, caplog):
    settings, factory = runtime_environment
    requests = []

    def reply(request):
        requests.append(request)
        return httpx.Response(200, json={"ignored": "response-private-marker"})

    runtime = LineRuntime(settings, factory, transport=httpx.MockTransport(reply))
    assert runtime.enabled and isinstance(runtime.sender, LiveLineReplySender)
    assert requests == []
    job_id = enqueue(runtime)
    result = runtime.tick()
    assert result["api_accepted"] == result["processed"] == 1
    assert result["fake_sent"] == 0 and len(requests) == 1
    assert str(requests[0].url) == "https://api.line.me/v2/bot/message/reply"
    assert requests[0].headers["authorization"] == "Bearer runtime-test-access-token"
    assert json.loads(requests[0].content)["replyToken"] == "runtime-test-reply-token"
    assert not any(runtime.tick().values()) and len(requests) == 1
    with factory() as db:
        receipt = db.scalar(select(LineReplyJob).where(LineReplyJob.id == job_id))
        assert receipt.status == "API_ACCEPTED" and receipt.attempt_count == 1
    visible = caplog.text + repr(runtime) + repr(runtime.sender) + repr(runtime.buffer)
    for value in (settings.line_channel_access_token, settings.line_channel_secret,
                  settings.secret_key, "runtime-test-reply-token", "runtime-private-user",
                  "response-private-marker"):
        assert value not in visible
    runtime.close()


def test_config_change_stops_live_dispatch(runtime_environment):
    settings, factory = runtime_environment
    requests = []
    runtime = LineRuntime(settings, factory, transport=httpx.MockTransport(
        lambda request: requests.append(request) or httpx.Response(200),
    ))
    enqueue(runtime)
    settings.line_reply_mode = "fake"
    assert not runtime.enabled and not any(runtime.tick().values())
    assert requests == []
    runtime.close()


def test_each_tick_dispatches_at_most_one_reply(runtime_environment):
    settings, factory = runtime_environment
    requests = []
    runtime = LineRuntime(settings, factory, transport=httpx.MockTransport(
        lambda request: requests.append(request.url.path) or httpx.Response(200),
    ))
    enqueue(runtime, event_id="runtime-bounded-event-1")
    enqueue(runtime, event_id="runtime-bounded-event-2")
    assert runtime.tick()["processed"] == 1
    assert len(requests) == 1 and len(runtime.buffer.pending_ids()) == 1
    assert runtime.tick()["processed"] == 1
    assert len(requests) == 2 and not runtime.buffer.pending_ids()
    runtime.close()


def test_close_wipes_shared_memory_and_cannot_restart(runtime_environment, monkeypatch):
    settings, factory = runtime_environment
    sender = LiveLineReplySender("test-token", transport=httpx.MockTransport(
        lambda _request: pytest.fail("Closed runtime must not dispatch"),
    ))
    runtime = LineRuntime(settings, factory, sender=sender)
    assert runtime.sender is sender and runtime.settings is settings and runtime.factory is factory
    enqueue(runtime)
    store = runtime.buffer.conversations
    store.sessions["test-actor"] = {"transient": True}
    store.handoffs["test-handoff"] = {"transient": True}
    cleared = []
    original_clear = runtime.case_handoffs.clear

    def clear():
        cleared.append(True)
        original_clear()

    monkeypatch.setattr(runtime.case_handoffs, "clear", clear)
    runtime.close()
    assert not runtime.enabled and runtime.sender is None
    assert runtime.buffer.pending_ids() == []
    assert not store.sessions and not store.handoffs and cleared == [True]
    assert not any(runtime.tick().values())
    runtime.close()
