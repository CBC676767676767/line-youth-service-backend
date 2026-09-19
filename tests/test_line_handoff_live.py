"""Real live handoff wiring, with synthetic identities and no external HTTP transport."""

import json
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import line_handoff, line_runtime, precheck
from app.auth_crypto import digest
from app.bootstrap import seed_scheme
from app.config import Settings
from app.db import utcnow
from app.line_conversation import STATE_TTL_SECONDS, build_reply
from app.line_handoff import CaseHandoffRegistry
from app.main import create_app
from app.models import Account, AuthSession, Case, CaseAccess, RoleGrant
from app.precheck_engine import DEFAULT_BUNDLE_PATH, load_bundle
from app.precheck_models import PrecheckSnapshot


@pytest.fixture
def handoff_api(tmp_path, monkeypatch):
    now = utcnow()
    monkeypatch.setattr(line_handoff, "utcnow", lambda: now)
    settings = Settings(
        _env_file=None, app_env="test", database_url=f"sqlite:///{tmp_path}/live-handoff.db",
        secret_key="synthetic-handoff-test-" * 3, totp_encryption_key=Fernet.generate_key().decode(),
        allowed_origins=["http://testserver"], public_origin="https://service.example",
        storage_dir=tmp_path / "files", precheck_rules_path=DEFAULT_BUNDLE_PATH,
        line_bot_enabled=True, line_reply_mode="live", line_simulator_enabled=False,
        line_channel_access_token="synthetic-live-access-token", line_channel_secret="synthetic-live-secret",
        line_messaging_channel_id="synthetic-live-channel", line_destination_user_id="U" + "1" * 32,
        line_public_precheck_url="https://service.example/precheck",
    )
    outgoing = []

    def transport(request):
        outgoing.append(request.url.path)
        return httpx.Response(200, json={})

    real_runtime = line_runtime.LineRuntime
    monkeypatch.setattr(line_runtime, "LineRuntime", lambda config, factory: real_runtime(
        config, factory, transport=httpx.MockTransport(transport),
    ))
    app = create_app(settings)
    factory = app.state.session_factory
    with factory() as db:
        seed_scheme(db)
        for actor, role in [("owner", "applicant"), ("other", "applicant"), ("supervisor", "supervisor")]:
            db.add(Account(id=actor, email=actor + "@example.test", email_verified_at=now))
            db.flush()
            db.add(RoleGrant(account_id=actor, role=role, scope_type="GLOBAL"))
            db.add(AuthSession(account_id=actor, token_hash=digest(settings.secret_key, "session", actor),
                               csrf_hash=digest(settings.secret_key, "csrf", "csrf-" + actor),
                               auth_method="staff_mfa" if actor == "supervisor" else "email_otp",
                               expires_at=now + timedelta(hours=1)))
        db.commit()
    with TestClient(app, client=("198.51.100.15", 40000), raise_server_exceptions=False) as client:
        def call(actor, method, path, body=None, **kwargs):
            headers = {"Origin": "http://testserver"}
            if actor:
                headers.update({"Cookie": f"{settings.session_cookie_name}={actor}",
                                "X-CSRF-Token": "csrf-" + actor})
            headers.update(kwargs.pop("headers", {}))
            return client.request(method, "/api/v1" + path, json=body, headers=headers, **kwargs)

        def draft(actor="owner"):
            response = call(actor, "POST", "/cases", {"scheme_id": "youth-demo"},
                            headers={"Idempotency-Key": str(uuid4())})
            assert response.status_code == 201, response.text
            return response.json()["data"]["id"], response.headers["ETag"]

        yield SimpleNamespace(app=app, factory=factory, client=client, settings=settings, now=now,
                              call=call, draft=draft, outgoing=outgoing, runtime=app.state.line_runtime)
    assert outgoing == []  # These handoff tests never enqueue or send a LINE message.


def actions(value):
    if isinstance(value, dict):
        if value.get("type") in {"postback", "uri"} and "label" in value:
            yield value
        for child in value.values():
            yield from actions(child)
    elif isinstance(value, list):
        for child in value:
            yield from actions(child)


def make_handoff(env, actor="synthetic-hashed-actor"):
    bundle = load_bundle(env.app.state.settings.precheck_rules_path)
    bundle = bundle.model_copy(update={"official_application_url":
                                       env.app.state.settings.precheck_official_application_url or bundle.official_application_url})
    store = env.runtime.buffer.conversations
    messages = build_reply("預檢", actor, bundle, env.settings.secret_key, env.now,
                           env.settings.line_public_precheck_url, store=store)
    for label in ("已經購買", "合成 AI 工作室", "月費", "官方"):
        command = next(item["data"] for item in actions(messages) if item["type"] == "postback" and label in item["label"])
        messages = build_reply(command, actor, bundle, env.settings.secret_key, env.now,
                               env.settings.line_public_precheck_url, store=store)
    url = next(item["uri"] for item in actions(messages) if "line_handoff=" in item.get("uri", ""))
    return parse_qs(urlsplit(url).query)["line_handoff"][0], store, actor


def answers():
    return {"purchase_stage": "purchased", "tool_id": "demo-studio", "plan_id": "monthly-credit",
            "billing_type": "monthly", "purchase_channel": "official", "purchase_url": "https://studio.example",
            "purchase_date": "2026-09-01", "subscription_start": "2026-09-01", "subscription_end": "2026-10-01",
            "residency": "hsinchu", "birth_date": "2000-01-01", "application_type": "standard", "payer": "self",
            "prior_subsidy": "none", "seller": "SYNTHETIC_PRIVATE_SELLER"}


def save(env, token, case_id, tag, *, actor="owner", key=None):
    return env.call(actor, "POST", f"/cases/{case_id}/precheck", answers(), headers={
        "X-Line-Handoff": token, "If-Match": tag, "Idempotency-Key": key or str(uuid4()),
    })


def test_live_handoff_works_from_remote_origin_with_public_choices_only(handoff_api):
    env = handoff_api
    token, _, _ = make_handoff(env)
    response = env.call(None, "GET", "/line/handoffs/" + token)
    assert response.status_code == 200, response.text
    assert set(response.json()["data"]["choices"]) == line_handoff.CHOICE_FIELDS
    assert response.json()["data"]["choices"]["tool_id"] == "demo-studio"
    assert "no-store" in response.headers["Cache-Control"]
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert not hasattr(env.app.state, "line_simulator")
    assert env.call(None, "GET", "/line/results/" + token).status_code == 401
    with env.factory() as db:
        assert db.scalar(select(func.count()).select_from(Case)) == 0
        assert db.scalar(select(func.count()).select_from(PrecheckSnapshot)) == 0


def test_configured_official_url_override_matches_reply_version(handoff_api):
    env = handoff_api
    env.app.state.settings.precheck_official_application_url = "https://official.example/application"
    token, _, _ = make_handoff(env)
    assert env.call(None, "GET", "/line/handoffs/" + token).status_code == 200


def test_anonymous_result_is_unsaved_and_safe_then_committed_save_binds_only_owner(handoff_api):
    env = handoff_api
    token, store, actor = make_handoff(env)
    response = env.call(None, "POST", "/precheck/evaluate", answers(), headers={"X-Line-Handoff": token})
    assert response.status_code == 200
    safe = store.sessions[actor]["safe_result"]
    assert safe["saved"] is False
    assert env.runtime.case_handoffs.get(token, now=env.now) is None
    for value in ("2000-01-01", "SYNTHETIC_PRIVATE_SELLER", "purchase_url", "prepared_documents"):
        assert value not in json.dumps(safe)
    case_id, tag = env.draft()
    saved = save(env, token, case_id, tag)
    assert saved.status_code == 201, saved.text
    snapshot = saved.json()["data"]["snapshot"]
    safe = store.sessions[actor]["safe_result"]
    assert safe["saved"] is True
    assert safe["summary"] == {key: snapshot["result"]["summary"][key]
                                for key in ("required_total", "completed", "incomplete", "issues")}
    assert case_id not in json.dumps(safe) and snapshot["id"] not in json.dumps(safe)
    result = env.call("owner", "GET", "/line/results/" + token)
    assert result.status_code == 200
    assert result.json()["data"] == {"case_id": case_id, "snapshot_id": snapshot["id"]}
    assert env.call("other", "GET", "/line/results/" + token).status_code == 404
    assert env.call("supervisor", "GET", "/line/results/" + token).status_code == 404
    other_case, other_tag = env.draft("other")
    assert save(env, token, other_case, other_tag, actor="other").status_code == 201
    assert env.runtime.case_handoffs.get(token, now=env.now).owner_id == "owner"
    assert env.call("owner", "GET", "/line/results/" + token).json()["data"]["case_id"] == case_id
    assert store.sessions[actor]["safe_result"] == safe
    # A later web evaluation is explicitly unsaved; it does not change the bound owner.
    assert env.call(None, "POST", "/precheck/evaluate", answers(), headers={"X-Line-Handoff": token}).status_code == 200
    assert store.sessions[actor]["safe_result"]["saved"] is False
    assert env.runtime.case_handoffs.get(token, now=env.now).owner_id == "owner"


def test_commit_failure_does_not_bind_or_publish_saved_counts(handoff_api, monkeypatch):
    env = handoff_api
    token, store, actor = make_handoff(env)
    case_id, tag = env.draft()

    def fail_finish(*_args, **_kwargs):
        raise RuntimeError("synthetic rollback")

    monkeypatch.setattr(precheck, "idem_finish", fail_finish)
    response = save(env, token, case_id, tag)
    assert response.status_code == 500
    assert env.runtime.case_handoffs.get(token, now=env.now) is None
    assert store.sessions[actor]["safe_result"] is None
    with env.factory() as db:
        assert db.scalar(select(func.count()).select_from(PrecheckSnapshot)) == 0


def test_access_checked_idempotent_replay_can_bind_after_save_but_not_bypass_revocation(handoff_api):
    env = handoff_api
    token, _, _ = make_handoff(env)
    case_id, tag = env.draft()
    key = str(uuid4())
    first = env.call("owner", "POST", f"/cases/{case_id}/precheck", answers(), headers={
        "If-Match": tag, "Idempotency-Key": key,
    })
    assert first.status_code == 201
    assert env.runtime.case_handoffs.get(token, now=env.now) is None
    replay = save(env, token, case_id, tag, key=key)
    assert replay.status_code == 201
    assert env.call("owner", "GET", "/line/results/" + token).status_code == 200
    with env.factory() as db:
        db.scalar(select(CaseAccess).where(CaseAccess.case_id == case_id)).revoked_at = utcnow()
        db.commit()
    assert env.call("owner", "GET", "/line/results/" + token).status_code == 404
    assert save(env, token, case_id, tag, key=key).status_code == 404


@pytest.mark.parametrize("invalidate", ["expiry", "restart", "policy"])
def test_expired_restarted_or_changed_policy_token_does_not_resolve_saved_case(handoff_api, monkeypatch, tmp_path, invalidate):
    env = handoff_api
    token, store, actor = make_handoff(env)
    case_id, tag = env.draft()
    assert save(env, token, case_id, tag).status_code == 201
    if invalidate == "expiry":
        monkeypatch.setattr(line_handoff, "utcnow", lambda: env.now + timedelta(seconds=STATE_TTL_SECONDS + 1))
    elif invalidate == "restart":
        build_reply("預檢", actor, load_bundle(DEFAULT_BUNDLE_PATH), env.settings.secret_key, env.now,
                    env.settings.line_public_precheck_url, store=store)
    else:
        new_policy = json.loads(DEFAULT_BUNDLE_PATH.read_text(encoding="utf-8"))
        new_policy["rules"]["version"] = "synthetic-changed-policy"
        new_path = tmp_path / "changed-policy.json"
        new_path.write_text(json.dumps(new_policy), encoding="utf-8")
        env.app.state.settings.precheck_rules_path = new_path
    assert env.call(None, "GET", "/line/handoffs/" + token).status_code == 410
    assert env.call("owner", "GET", "/line/results/" + token).status_code == 404


def test_failed_anonymous_evaluation_clears_previous_success_without_changing_case_binding(handoff_api):
    env = handoff_api
    token, store, actor = make_handoff(env)
    assert env.call(None, "POST", "/precheck/evaluate", answers(), headers={"X-Line-Handoff": token}).status_code == 200
    response = env.call(None, "POST", "/precheck/evaluate", {"passed": True}, headers={"X-Line-Handoff": token})
    assert response.status_code == 422
    assert store.sessions[actor]["safe_result"]["outcome"] == "failed"
    assert env.runtime.case_handoffs.get(token, now=env.now) is None


def test_registry_is_bounded_non_replaceable_and_does_not_extend_ttl():
    registry = CaseHandoffRegistry(max_items=1)
    now = utcnow()
    token = "A" * 32
    expiry = now + timedelta(minutes=5)
    assert registry.bind(token, owner_id="owner", case_id="case", snapshot_id="snapshot", expires_at=expiry, now=now)
    assert not registry.bind(token, owner_id="other", case_id="other-case", snapshot_id="other-snapshot", expires_at=expiry, now=now)
    assert not registry.bind("B" * 32, owner_id="other", case_id="other-case", snapshot_id="other-snapshot", expires_at=expiry, now=now)
    assert registry.bind(token, owner_id="owner", case_id="case", snapshot_id="new-snapshot",
                         expires_at=expiry + timedelta(minutes=5), now=now)
    assert registry.get(token, now=now).expires_at == expiry
    assert "owner" not in repr(registry) and "owner" not in repr(registry.get(token, now=now))
    assert registry.get(token, now=expiry) is None
    registry.clear()
