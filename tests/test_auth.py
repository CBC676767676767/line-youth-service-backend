from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from email import policy
from email.parser import BytesParser
import re
import stat
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app import auth
from app.auth_crypto import encrypt_totp_secret, hash_password, totp_at
from app.common import ApiError
from app.config import Settings
from app.db import Base, make_engine, make_session_factory, new_id, utcnow
from app.mail import MailDeliveryError
from app.models import Account, AuthSession, EmailChallenge, IdentityLink, LineChallenge, RoleGrant


@pytest.fixture
def auth_env(tmp_path):
    settings = Settings(
        app_env="test", secret_key="testing-secret-" * 4,
        totp_encryption_key=Fernet.generate_key().decode(),
        database_url=f"sqlite:///{tmp_path / 'auth.db'}",
        public_origin="https://service.test", allowed_origins=["https://service.test"],
        cookie_secure=True, mail_spool_dir=tmp_path / "mail",
        line_channel_id="12345", line_provider_id="provider-1",
    )
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = factory
    app.include_router(auth.router, prefix="/api/v1")

    @app.exception_handler(ApiError)
    async def api_error(_request, error):
        return JSONResponse({"error": {"code": error.code, "message": error.message}},
                            status_code=error.status, headers=getattr(error, "headers", None))

    @app.get("/staff-only")
    def staff_only(principal=Depends(auth.require_roles("supervisor"))):
        return {"account_id": principal.account.id}

    with TestClient(app, base_url="https://service.test", headers={"Origin": "https://service.test"}) as client:
        yield SimpleNamespace(app=app, client=client, settings=settings, factory=factory)
    engine.dispose()


def _latest_code(env):
    path = max(env.settings.mail_spool_dir.glob("*.eml"), key=lambda item: item.stat().st_mtime_ns)
    message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    body = message.get_body(preferencelist=("plain",)).get_content()
    return re.search(r"驗證碼：([0-9]{6})", body).group(1)


def _challenge(env, email="youth@example.org", client=None):
    client = client or env.client
    response = client.post("/api/v1/auth/email/challenges", json={"email": email})
    assert response.status_code == 202, response.text
    return response.json()["data"], _latest_code(env)


def _login(env, email="youth@example.org", client=None):
    client = client or env.client
    data, code = _challenge(env, email, client)
    response = client.post("/api/v1/auth/email/challenges/verify", json={"challenge_id": data["challenge_id"], "code": code})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["data"]["csrf_token"]
    return response.json()["data"]


def _staff(env):
    secret = "JBSWY3DPEHPK3PXP"
    with env.factory() as db:
        account = Account(id=new_id(), email="staff@example.org", email_verified_at=utcnow(),
                          password_hash=hash_password("correct horse battery staple"),
                          totp_secret_encrypted=encrypt_totp_secret(secret, env.settings.secret_key, env.settings.totp_encryption_key))
        db.add(account)
        db.flush()
        db.add(RoleGrant(account_id=account.id, role="supervisor", scope_type="GLOBAL"))
        db.commit()
        account_id = account.id
    return account_id, secret


def _line_claims(env, *, subject="line-user-1", channel=None):
    return {"iss": "https://access.line.me", "aud": channel or env.settings.line_channel_id,
            "sub": subject, "exp": int(utcnow().timestamp()) + 600, "iat": int(utcnow().timestamp())}


def _mock_line(monkeypatch, claims):
    def post(url, *, data, **kwargs):
        assert url == "https://api.line.me/oauth2/v2.1/verify"
        assert data["client_id"] == "12345"
        assert "id_token" in data
        assert kwargs["follow_redirects"] is False
        return SimpleNamespace(status_code=200, json=lambda: claims)
    monkeypatch.setattr(auth.httpx, "post", post)


def _bind(env, client=None):
    client = client or env.client
    challenge = client.post("/api/v1/identity-links/line/challenges")
    assert challenge.status_code == 201, challenge.text
    return client.post("/api/v1/identity-links/line", json={
        "challenge_id": challenge.json()["data"]["challenge_id"], "id_token": "line-id-token",
    })


def test_email_delivery_is_private_and_code_never_returned(auth_env):
    data, code = _challenge(auth_env)
    assert "code" not in data and data["code_length"] == 6
    path = next(auth_env.settings.mail_spool_dir.glob("*.eml"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    with auth_env.factory() as db:
        challenge = db.get(EmailChallenge, data["challenge_id"])
        assert challenge.code_hash != code and len(challenge.code_hash) == 64
        assert db.scalar(select(func.count()).select_from(Account)) == 0


def test_otp_success_and_replay(auth_env):
    data, code = _challenge(auth_env)
    payload = {"challenge_id": data["challenge_id"], "code": code}
    response = auth_env.client.post("/api/v1/auth/email/challenges/verify", json=payload)
    assert response.status_code == 200
    assert response.json()["data"]["identity_status"] == "EMAIL_VERIFIED"
    assert response.json()["data"]["roles"] == ["applicant"]
    assert "HttpOnly" in response.headers.get_list("set-cookie")[0]
    assert "Secure" in response.headers.get_list("set-cookie")[0]
    assert auth_env.client.post("/api/v1/auth/email/challenges/verify", json=payload).status_code == 401


def test_expired_otp_cannot_create_account(auth_env):
    data, code = _challenge(auth_env)
    with auth_env.factory() as db:
        db.get(EmailChallenge, data["challenge_id"]).expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    response = auth_env.client.post("/api/v1/auth/email/challenges/verify", json={"challenge_id": data["challenge_id"], "code": code})
    assert response.status_code == 410
    with auth_env.factory() as db:
        assert db.scalar(select(func.count()).select_from(Account)) == 0


def test_otp_attempt_limit_is_persistent(auth_env):
    data, code = _challenge(auth_env)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        assert auth_env.client.post("/api/v1/auth/email/challenges/verify", json={"challenge_id": data["challenge_id"], "code": wrong}).status_code == 401
    assert auth_env.client.post("/api/v1/auth/email/challenges/verify", json={"challenge_id": data["challenge_id"], "code": code}).status_code == 401


def test_resend_is_rate_limited_and_preserves_existing_expiry(auth_env):
    data, _ = _challenge(auth_env)
    response = auth_env.client.post("/api/v1/auth/email/challenges", json={"email": "youth@example.org"})
    assert response.status_code == 429 and int(response.headers["retry-after"]) > 0
    with auth_env.factory() as db:
        assert db.get(EmailChallenge, data["challenge_id"]).consumed_at is None
        assert db.scalar(select(func.count()).select_from(EmailChallenge)) == 1


def test_source_limit_cannot_be_spoofed_with_forwarded_header(auth_env):
    for index in range(10):
        response = auth_env.client.post("/api/v1/auth/email/challenges", json={"email": f"user{index}@example.org"}, headers={"X-Forwarded-For": f"192.0.2.{index}"})
        assert response.status_code == 202
    assert auth_env.client.post("/api/v1/auth/email/challenges", json={"email": "last@example.org"}, headers={"X-Forwarded-For": "198.51.100.1"}).status_code == 429


def test_cookie_mutations_require_csrf_and_trusted_origin(auth_env):
    data = _login(auth_env)
    del auth_env.client.headers["X-CSRF-Token"]
    assert auth_env.client.post("/api/v1/auth/logout").status_code == 403
    auth_env.client.headers["X-CSRF-Token"] = data["csrf_token"]
    assert auth_env.client.post("/api/v1/auth/logout", headers={"Origin": "https://attacker.test"}).status_code == 403
    assert auth_env.client.post("/api/v1/auth/logout").status_code == 204
    assert auth_env.client.get("/api/v1/me").status_code == 401


def test_deactivated_account_invalidates_existing_session(auth_env):
    data = _login(auth_env)
    with auth_env.factory() as db:
        db.get(Account, data["account"]["id"]).status = "DISABLED"
        db.commit()
    assert auth_env.client.get("/api/v1/me").status_code == 401


def test_role_version_change_invalidates_existing_session(auth_env):
    data = _login(auth_env)
    with auth_env.factory() as db:
        db.get(Account, data["account"]["id"]).role_version += 1
        db.commit()
    assert auth_env.client.get("/api/v1/me").status_code == 401


def test_staff_requires_mfa_and_totp_cannot_be_replayed(auth_env):
    _, secret = _staff(auth_env)
    credentials = {"identifier": "staff@example.org", "password": "correct horse battery staple"}
    response = auth_env.client.post("/api/v1/auth/staff/session", json=credentials)
    assert response.status_code == 200
    assert auth_env.client.get("/api/v1/me").status_code == 401
    code = totp_at(secret, int(utcnow().timestamp() // 30))
    payload = {"mfa_challenge_id": response.json()["data"]["mfa_challenge_id"], "code": code}
    login = auth_env.client.post("/api/v1/auth/staff/mfa", json=payload)
    assert login.status_code == 200 and login.json()["data"]["roles"] == ["supervisor"]
    assert "password_hash" not in login.text and "totp_secret" not in login.text
    assert auth_env.client.get("/staff-only").status_code == 200
    assert auth_env.client.post("/api/v1/auth/staff/mfa", json=payload).status_code == 401
    second = auth_env.client.post("/api/v1/auth/staff/session", json=credentials)
    payload["mfa_challenge_id"] = second.json()["data"]["mfa_challenge_id"]
    assert auth_env.client.post("/api/v1/auth/staff/mfa", json=payload).status_code == 401


def test_email_login_cannot_bypass_staff_mfa(auth_env):
    _staff(auth_env)
    data, code = _challenge(auth_env, "staff@example.org")
    response = auth_env.client.post("/api/v1/auth/email/challenges/verify", json={"challenge_id": data["challenge_id"], "code": code})
    assert response.status_code == 403 and response.json()["error"]["code"] == "STAFF_LOGIN_REQUIRED"
    assert auth_env.client.get("/staff-only").status_code == 401


def test_mixed_role_account_email_session_does_not_receive_staff_grants(auth_env):
    account_id, _ = _staff(auth_env)
    with auth_env.factory() as db:
        db.add(RoleGrant(account_id=account_id, role="applicant", scope_type="GLOBAL"))
        db.commit()
    result = _login(auth_env, "staff@example.org")
    assert result["roles"] == ["applicant"]
    assert auth_env.client.get("/staff-only").status_code == 403


def test_line_wrong_channel_is_rejected(auth_env, monkeypatch):
    _login(auth_env)
    _mock_line(monkeypatch, _line_claims(auth_env, channel="wrong-channel"))
    response = _bind(auth_env)
    assert response.status_code == 401
    with auth_env.factory() as db:
        assert db.scalar(select(func.count()).select_from(IdentityLink)) == 0


def test_line_missing_configuration_is_explicit(auth_env):
    auth_env.settings.line_channel_id = ""
    response = auth_env.client.post("/api/v1/auth/line/session", json={"id_token": "some-token"})
    assert response.status_code == 503 and response.json()["error"]["code"] == "LINE_NOT_CONFIGURED"


def test_line_binding_cannot_move_to_another_account(auth_env, monkeypatch):
    _login(auth_env)
    _mock_line(monkeypatch, _line_claims(auth_env))
    assert _bind(auth_env).status_code == 201
    with TestClient(auth_env.app, base_url="https://service.test", headers={"Origin": "https://service.test"}) as other:
        _login(auth_env, "other@example.org", other)
        assert _bind(auth_env, other).status_code == 409
    with auth_env.factory() as db:
        assert db.scalar(select(func.count()).select_from(IdentityLink)) == 1


def test_expired_line_challenge_is_rejected(auth_env, monkeypatch):
    _login(auth_env)
    _mock_line(monkeypatch, _line_claims(auth_env))
    response = auth_env.client.post("/api/v1/identity-links/line/challenges")
    identifier = response.json()["data"]["challenge_id"]
    with auth_env.factory() as db:
        db.get(LineChallenge, identifier).expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert auth_env.client.post("/api/v1/identity-links/line", json={"challenge_id": identifier, "id_token": "line-token"}).status_code == 410


def test_line_login_requires_service_reauth_before_unlink(auth_env, monkeypatch):
    _login(auth_env)
    _mock_line(monkeypatch, _line_claims(auth_env))
    bound = _bind(auth_env)
    login = auth_env.client.post("/api/v1/auth/line/session", json={"id_token": "line-token"})
    assert login.status_code == 200
    auth_env.client.headers["X-CSRF-Token"] = login.json()["data"]["csrf_token"]
    response = auth_env.client.delete("/api/v1/identity-links/line", headers={"If-Match": bound.headers["etag"]})
    assert response.status_code == 403 and response.json()["error"]["code"] == "REAUTH_REQUIRED"


def test_unlink_revokes_sessions_and_preserves_account(auth_env, monkeypatch):
    data = _login(auth_env)
    _mock_line(monkeypatch, _line_claims(auth_env))
    bound = _bind(auth_env)
    assert auth_env.client.delete("/api/v1/identity-links/line", headers={"If-Match": '"wrong-version"'}).status_code == 412
    assert auth_env.client.delete("/api/v1/identity-links/line", headers={"If-Match": bound.headers["etag"]}).status_code == 204
    assert auth_env.client.get("/api/v1/me").status_code == 401
    with auth_env.factory() as db:
        assert db.get(Account, data["account"]["id"]).status == "ACTIVE"
        assert db.scalar(select(IdentityLink)).revoked_at is not None
        assert all(row.revoked_at for row in db.scalars(select(AuthSession)))


def test_email_change_requires_new_mailbox_code_and_rotates_session(auth_env):
    _login(auth_env)
    old_session_cookie = auth_env.client.cookies.get(auth_env.settings.session_cookie_name)
    challenge = auth_env.client.post("/api/v1/account/email/change-challenges", json={"email": "new-address@example.org"})
    assert challenge.status_code == 202
    response = auth_env.client.post("/api/v1/account/email/change", json={"challenge_id": challenge.json()["data"]["challenge_id"], "code": _latest_code(auth_env)})
    assert response.status_code == 200 and response.json()["data"]["account"]["email"] == "new-address@example.org"
    assert auth_env.client.cookies.get(auth_env.settings.session_cookie_name) != old_session_cookie
    with TestClient(auth_env.app, base_url="https://service.test") as old:
        old.cookies.set(auth_env.settings.session_cookie_name, old_session_cookie)
        assert old.get("/api/v1/me").status_code == 401


def test_mail_delivery_failure_is_not_success(auth_env, monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise MailDeliveryError("transport failed")
    monkeypatch.setattr(auth, "send_otp", unavailable)
    response = auth_env.client.post("/api/v1/auth/email/challenges", json={"email": "youth@example.org"})
    assert response.status_code == 503 and response.json()["error"]["code"] == "MAIL_UNAVAILABLE"
    with auth_env.factory() as db:
        assert db.scalar(select(EmailChallenge)).consumed_at is not None


def test_page_reload_recovers_csrf_without_exposing_session_token(auth_env):
    login = _login(auth_env)
    del auth_env.client.headers["X-CSRF-Token"]
    response = auth_env.client.get("/api/v1/me")
    assert response.status_code == 200
    assert response.json()["data"]["csrf_token"] == login["csrf_token"]
    assert auth_env.client.cookies.get(auth_env.settings.session_cookie_name) not in response.text
    auth_env.client.headers["X-CSRF-Token"] = response.json()["data"]["csrf_token"]
    assert auth_env.client.post("/api/v1/auth/logout").status_code == 204


def test_sqlite_parallel_session_touch_does_not_conflict_or_change_version(auth_env):
    _login(auth_env)
    with auth_env.factory() as db:
        row = db.scalar(select(AuthSession))
        row.last_seen_at = utcnow() - timedelta(seconds=45)
        db.commit()
        identifier, version, last_seen = row.id, row.version, row.last_seen_at
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: auth_env.client.get("/api/v1/me"), range(2)))
    assert [response.status_code for response in responses] == [200, 200]
    with auth_env.factory() as db:
        row = db.get(AuthSession, identifier)
        assert row.last_seen_at > last_seen
        assert row.version == version


def test_account_sessions_are_owned_and_revocable(auth_env):
    _login(auth_env)
    own = auth_env.client.get("/api/v1/account/sessions").json()["data"]
    assert len(own) == 1 and own[0]["is_current"]
    with TestClient(auth_env.app, base_url="https://service.test", headers={"Origin": "https://service.test"}) as other:
        _login(auth_env, "other@example.org", other)
        target = other.get("/api/v1/account/sessions").json()["data"][0]
        assert auth_env.client.delete(f"/api/v1/account/sessions/{target['id']}", headers={"If-Match": target["etag"]}).status_code == 404
    assert auth_env.client.delete(f"/api/v1/account/sessions/{own[0]['id']}", headers={"If-Match": own[0]["etag"]}).status_code == 204
    assert auth_env.client.get("/api/v1/me").status_code == 401


def test_relink_never_reuses_notification_binding_version(auth_env, monkeypatch):
    _login(auth_env)
    _mock_line(monkeypatch, _line_claims(auth_env))
    first = _bind(auth_env)
    original_version = first.json()["data"]["binding_version"]
    assert auth_env.client.delete("/api/v1/identity-links/line", headers={"If-Match": first.headers["etag"]}).status_code == 204
    real_now = utcnow
    monkeypatch.setattr(auth, "utcnow", lambda: real_now() + timedelta(seconds=61))
    _login(auth_env)
    _mock_line(monkeypatch, _line_claims(auth_env, subject="replacement-line-account"))
    second = _bind(auth_env)
    assert second.status_code == 201 and second.json()["data"]["binding_version"] > original_version
    with auth_env.factory() as db:
        assert db.scalar(select(IdentityLink).where(
            IdentityLink.revoked_at.is_(None), IdentityLink.binding_version == original_version
        )) is None
