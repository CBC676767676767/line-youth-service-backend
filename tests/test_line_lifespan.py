"""Explicit live configuration and isolated reply-worker application lifecycle."""

import asyncio
from threading import Event

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import Settings
from app.line_replies import LineReplyBuffer
from app.main import create_app
from test_line_replies import event, jobs, webhook


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Lifecycle tests must never use real network transport")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)


def configuration(tmp_path, **overrides):
    values = dict(_env_file=None, app_env="test", database_url=f"sqlite:///{tmp_path / 'lifecycle.db'}",
                  secret_key="synthetic-lifecycle-key-" * 3,
                  totp_encryption_key=Fernet.generate_key().decode(),
                  storage_dir=tmp_path / "files", mail_spool_dir=tmp_path / "mail",
                  worker_poll_seconds=0.01)
    values.update(overrides)
    return Settings(**values)


def live_configuration(tmp_path, **overrides):
    values = dict(line_bot_enabled=True, line_reply_mode="live",
                  line_channel_access_token="synthetic-live-token", line_channel_secret="synthetic-live-secret",
                  line_messaging_channel_id="synthetic-channel", line_destination_user_id="bot-id",
                  line_public_precheck_url="https://youth.example.test/precheck")
    values.update(overrides)
    return configuration(tmp_path, **values)


@pytest.mark.parametrize("field", ["line_channel_access_token", "line_channel_secret",
                                   "line_messaging_channel_id", "line_destination_user_id"])
@pytest.mark.parametrize("value", ["", "contains whitespace"])
def test_enabled_live_configuration_requires_complete_channel(tmp_path, field, value):
    with pytest.raises(ValueError, match="Live LINE replies require channel"):
        live_configuration(tmp_path, **{field: value})


@pytest.mark.parametrize("url", ["", "http://localhost/precheck", "https://", "https://example.test:bad",
                                 "https://user:secret@example.test", "https://example.test/\nprivate",
                                 "https://example.test\\@other.test/precheck"])
def test_live_handoff_url_requires_unambiguous_https(tmp_path, url):
    with pytest.raises(ValueError):
        live_configuration(tmp_path, line_public_precheck_url=url)


@pytest.mark.parametrize("mode", ["disabled", "fake", "live"])
def test_disabled_bot_never_starts_reply_worker_or_simulator(tmp_path, mode):
    settings = configuration(tmp_path, line_reply_mode=mode)
    assert not settings.line_bot_enabled and not settings.line_simulator_enabled
    app = create_app(settings)
    assert app.state.line_reply_buffer is app.state.line_runtime.buffer
    with TestClient(app) as client:
        assert app.state.line_reply_task is None
        assert client.get("/line-simulator").status_code == 404
        assert client.get("/static/line-simulator.html").status_code == 404
        assert client.post("/api/v1/line/simulator/sessions").status_code == 404
        assert client.get("/health/live").status_code == 200
        assert "/api/v1/line/handoffs/{token}" in app.openapi()["paths"]
        assert client.get("/api/v1/line/handoffs/" + "A" * 32).status_code == 410
    assert not app.state.line_runtime.enabled and not app.state.line_reply_buffer.pending_ids()


def test_fake_mode_never_starts_live_background_dispatch(tmp_path):
    app = create_app(configuration(tmp_path, line_bot_enabled=True, line_reply_mode="fake"))
    with TestClient(app):
        assert app.state.line_reply_task is None and app.state.line_runtime.sender is None


def test_live_lifespan_replies_only_to_verified_inbound_tokens(tmp_path, monkeypatch):
    from app import line_runtime, worker
    runtime_class = line_runtime.LineRuntime
    sent = Event()
    requests = []

    def accepted(request):
        requests.append(request.url.path)
        sent.set()
        return httpx.Response(200)

    def runtime(settings, factory):
        return runtime_class(settings, factory, transport=httpx.MockTransport(accepted))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("The general notification worker must not run")

    monkeypatch.setattr(line_runtime, "LineRuntime", runtime)
    monkeypatch.setattr(worker, "run_once", forbidden)
    monkeypatch.setattr(worker, "process_notifications", forbidden)
    settings = live_configuration(tmp_path)
    app = create_app(settings)
    assert not requests
    with TestClient(app) as client:
        assert app.state.line_reply_task is not None
        assert webhook(client, settings, [event()], corrupt=True).status_code == 401
        assert not requests
        assert webhook(client, settings, [event()]).status_code == 200
        assert sent.wait(3)
    assert requests == ["/v2/bot/message/reply"]
    assert jobs(app.state.session_factory)[0].status == "API_ACCEPTED"
    assert app.state.line_reply_task.done() and not app.state.line_runtime.enabled
    assert not app.state.line_reply_buffer.pending_ids()


def test_poll_failure_recovers_without_logging_private_exception(tmp_path, monkeypatch, caplog):
    from app import line_runtime

    class Runtime:
        def __init__(self, *_args):
            self.enabled = True
            self.buffer = LineReplyBuffer()
            self.done = Event()
            self.calls = 0
            self.closed = False
            self.ran_on_event_loop = False

        def tick(self):
            try:
                asyncio.get_running_loop()
                self.ran_on_event_loop = True
            except RuntimeError:
                pass
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("synthetic-private-token-must-not-log")
            self.done.set()

        def close(self):
            self.closed = True
            self.enabled = False
            self.buffer.clear()

    monkeypatch.setattr(line_runtime, "LineRuntime", Runtime)
    app = create_app(live_configuration(tmp_path))
    with TestClient(app):
        assert app.state.line_runtime.done.wait(3)
    assert app.state.line_runtime.closed and not app.state.line_runtime.ran_on_event_loop
    assert app.state.line_reply_task.done()
    assert "line_reply_tick_failed type=RuntimeError" in caplog.text
    assert "synthetic-private-token-must-not-log" not in caplog.text


def test_shutdown_waits_for_inflight_tick_before_clearing_owner(tmp_path, monkeypatch):
    from app import line_runtime

    class Runtime:
        def __init__(self, *_args):
            self.enabled = True
            self.buffer = LineReplyBuffer()
            self.started, self.release = Event(), Event()
            self.finished = False
            self.closed = False

        def tick(self):
            self.started.set()
            assert self.release.wait(3)
            self.finished = True

        def close(self):
            assert self.finished
            self.closed = True
            self.buffer.clear()

    monkeypatch.setattr(line_runtime, "LineRuntime", Runtime)
    app = create_app(live_configuration(tmp_path))

    async def lifecycle():
        async with app.router.lifespan_context(app):
            assert await asyncio.to_thread(app.state.line_runtime.started.wait, 3)

            async def finish_current_tick():
                await asyncio.sleep(0.01)
                assert not app.state.line_runtime.closed
                app.state.line_runtime.release.set()

            finishing = asyncio.create_task(finish_current_tick())
        await finishing
        assert app.state.line_runtime.closed and app.state.line_reply_task.done()

    asyncio.run(lifecycle())
