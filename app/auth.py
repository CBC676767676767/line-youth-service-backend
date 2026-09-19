"""Account authentication, server-side sessions and LINE identity links."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hmac
import math
from pathlib import Path
import secrets
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth_crypto import decrypt_totp_secret, digest, hash_password, match_totp_step, verify_password
from .auth_limits import consume_limits
from .common import ApiError, audit, check_version, etag, new_id, ok, utcnow
from .db import get_db
from .mail import MailConfig, MailDeliveryError, send_otp
from .models import Account, AuthSession, EmailChallenge, IdentityLink, LineChallenge, MfaChallenge, RoleGrant


router = APIRouter(tags=["authentication"])
STAFF_ROLES = {"reviewer", "supervisor", "admin", "auditor"}
_DUMMY_PASSWORD = hash_password(secrets.token_urlsafe(24))


@dataclass
class Principal:
    account: Account
    session: AuthSession
    grants: list[RoleGrant]
    roles: set[str]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EmailChallengeInput(Input):
    email: EmailStr = Field(max_length=254)
    purpose: Literal["login", "reauth"] = "login"


class VerifyEmailInput(Input):
    challenge_id: str = Field(min_length=1, max_length=128)
    code: str = Field(pattern=r"^[0-9]{6}$")


class StaffLoginInput(Input):
    identifier: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=1024)

    @field_validator("password", mode="before")
    @classmethod
    def preserve_password(cls, value):
        # Whitespace can be an intentional part of a password.
        return value

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class VerifyMfaInput(Input):
    mfa_challenge_id: str = Field(min_length=1, max_length=128)
    code: str = Field(pattern=r"^[0-9]{6}$")


class LineTokenInput(Input):
    id_token: str = Field(min_length=1, max_length=16384)


class BindLineInput(LineTokenInput):
    challenge_id: str = Field(min_length=1, max_length=128)


class ChangeEmailInput(Input):
    email: EmailStr = Field(max_length=254)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _email(value: str) -> str:
    return value.strip().lower()


def _origin(request: Request) -> None:
    origin = request.headers.get("origin")
    allowed = {str(value).rstrip("/") for value in request.app.state.settings.allowed_origins}
    if origin and origin.rstrip("/") not in allowed:
        raise ApiError(403, "ORIGIN_NOT_ALLOWED", "此來源不允許執行帳號操作。")
    if not origin and request.headers.get("sec-fetch-site") == "cross-site":
        raise ApiError(403, "ORIGIN_NOT_ALLOWED", "此來源不允許執行帳號操作。")


def _source(request: Request) -> str:
    host = request.client.host if request.client else "unknown"
    return digest(request.app.state.settings.secret_key, "source-address", host)


def _throttle(request: Request, db: Session, action: str, identity: str = "") -> None:
    secret = request.app.state.settings.secret_key
    address = _source(request)
    key = digest(secret, "rate-identity", identity)
    if action == "email-send":
        rules = [(f"email:{key}", request.app.state.settings.otp_resend_seconds, 1), (f"email-hour:{key}", 3600, 10),
                 (f"send-ip:{address}", 60, 10), ("send-global", 60, 200)]
    else:
        rules = [(f"{action}-ip:{address}", 300, 60), (f"{action}-global", 60, 600)]
        if identity:
            rules.insert(0, (f"{action}-identity:{key}", 300, 20))
    consume_limits(db, rules, utcnow())


def _grants(db: Session, account_id: str) -> list[RoleGrant]:
    return list(db.scalars(select(RoleGrant).where(
        RoleGrant.account_id == account_id,
        RoleGrant.revoked_at.is_(None),
        or_(RoleGrant.expires_at.is_(None), RoleGrant.expires_at > utcnow()),
    )))


def _principal(db: Session, account: Account, session: AuthSession) -> Principal:
    grants = _grants(db, account.id)
    # A verified mailbox or LINE account must never bypass staff MFA.
    if session.auth_method != "staff_mfa":
        grants = [grant for grant in grants if grant.role == "applicant"]
    return Principal(account, session, grants, {grant.role for grant in grants})


def _authenticated_principal(request: Request, db: Session) -> Principal:
    settings = request.app.state.settings
    token = request.cookies.get(settings.session_cookie_name)
    if not token or len(token) > 256:
        raise ApiError(401, "AUTH_REQUIRED", "請先登入。")
    session = db.scalar(select(AuthSession).where(
        AuthSession.token_hash == digest(settings.secret_key, "session", token)
    ))
    now = utcnow()
    if not session or session.revoked_at is not None or _aware(session.expires_at) <= now:
        raise ApiError(401, "SESSION_EXPIRED", "登入已失效，請重新登入。")
    account = db.get(Account, session.account_id)
    if not account or account.status != "ACTIVE" or account.role_version != session.role_version:
        raise ApiError(401, "SESSION_REVOKED", "登入已失效，請重新登入。")
    idle = getattr(settings, "staff_idle_seconds", 900) if session.auth_method == "staff_mfa" else getattr(settings, "youth_idle_seconds", 1800)
    if _aware(session.last_seen_at) + timedelta(seconds=idle) <= now:
        raise ApiError(401, "SESSION_EXPIRED", "登入已失效，請重新登入。")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        _origin(request)
        csrf = request.headers.get("x-csrf-token", "")
        if not csrf or len(csrf) > 256 or not hmac.compare_digest(
            session.csrf_hash, digest(settings.secret_key, "csrf", csrf)
        ):
            raise ApiError(403, "CSRF_INVALID", "安全驗證失敗，請重新整理後再試。")
    principal = _principal(db, account, session)
    # End credential reads before the write-only touch transaction. SQLite cannot
    # safely upgrade two concurrent read transactions to writers. This is an
    # isolated auth Session, never the caller's business transaction.
    if _aware(session.last_seen_at) + timedelta(seconds=30) <= now:
        db.commit()
        db.execute(update(AuthSession).where(
            AuthSession.id == session.id, AuthSession.revoked_at.is_(None),
            AuthSession.last_seen_at < now - timedelta(seconds=30),
        ).values(last_seen_at=now))
        db.commit()
    return principal


def current_principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    # Credential lookup and idle-time maintenance use their own short transaction.
    # In particular, do not leave a SQLite read transaction open while a different
    # connection tries to commit the access-time update.
    with request.app.state.session_factory() as auth_db:
        verified = _authenticated_principal(request, auth_db)
    return Principal(
        db.merge(verified.account, load=False), db.merge(verified.session, load=False),
        verified.grants, verified.roles,
    )


def require_roles(*roles: str):
    def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.roles.intersection(roles):
            raise ApiError(403, "ROLE_REQUIRED", "您沒有此操作權限。")
        return principal
    return dependency


def _recent(principal: Principal, request: Request) -> None:
    seconds = getattr(request.app.state.settings, "reauth_seconds", 600)
    if not principal.session.reauth_at or _aware(principal.session.reauth_at) + timedelta(seconds=seconds) < utcnow():
        raise ApiError(403, "REAUTH_REQUIRED", "請先重新驗證目前帳號。")


def _active_account(account: Account | None) -> Account:
    if not account or account.status != "ACTIVE":
        raise ApiError(401, "ACCOUNT_UNAVAILABLE", "此帳號目前無法登入。")
    return account


def _cookie_response(response: Response, request: Request, token: str, csrf: str, max_age: int) -> None:
    settings = request.app.state.settings
    options = dict(secure=settings.cookie_secure, samesite="lax", path="/", max_age=max_age)
    response.set_cookie(settings.session_cookie_name, token, httponly=True, **options)
    response.set_cookie(f"{settings.session_cookie_name}_csrf", csrf, httponly=False, **options)


def _clear_cookies(response: Response, request: Request) -> None:
    name = request.app.state.settings.session_cookie_name
    response.delete_cookie(name, path="/")
    response.delete_cookie(f"{name}_csrf", path="/")


def _me(db: Session, principal: Principal, csrf_token: str | None = None) -> dict:
    account = principal.account
    link = db.scalar(select(IdentityLink).where(IdentityLink.account_id == account.id, IdentityLink.revoked_at.is_(None)))
    result = {
        "account": {"id": account.id, "email": account.email, "status": account.status,
                    "email_verified_at": account.email_verified_at},
        "roles": sorted(principal.roles),
        "identity_status": "EMAIL_VERIFIED" if account.email_verified_at else "UNVERIFIED",
        "line_link": {"id": link.id, "binding_version": link.binding_version, "version": link.version,
                      "etag": etag(link), "follow_status": link.observed_follow_status} if link else None,
        "permissions": [{"role": grant.role, "scope_type": grant.scope_type, "scope_id": grant.scope_id} for grant in principal.grants],
    }
    if csrf_token:
        result["csrf_token"] = csrf_token
    return result


def _new_session(db: Session, request: Request, account: Account, method: str):
    settings = request.app.state.settings
    seconds = getattr(settings, "staff_session_seconds", 28800) if method == "staff_mfa" else getattr(settings, "youth_session_seconds", 43200)
    token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    now = utcnow()
    session = AuthSession(
        id=new_id(), account_id=account.id,
        token_hash=digest(settings.secret_key, "session", token),
        csrf_hash=digest(settings.secret_key, "csrf", csrf),
        auth_method=method, role_version=account.role_version,
        last_seen_at=now, reauth_at=now if method != "line" else None,
        expires_at=now + timedelta(seconds=seconds),
    )
    db.add(session)
    return session, token, csrf, seconds


def _login_result(db: Session, request: Request, account: Account, method: str):
    session, token, csrf, seconds = _new_session(db, request, account, method)
    audit(db, request, actor_id=account.id, action="auth.login", resource_id=session.id, details={"method": method})
    db.commit()
    response = ok(request, _me(db, _principal(db, account, session), csrf))
    _cookie_response(response, request, token, csrf, seconds)
    return response


def _mail_config(request: Request) -> MailConfig:
    settings = request.app.state.settings
    return MailConfig(
        backend=settings.mail_backend, sender=settings.mail_from,
        spool_dir=Path(settings.mail_spool_dir),
        smtp_host=settings.smtp_host, smtp_port=settings.smtp_port,
        smtp_username=settings.smtp_username, smtp_password=settings.smtp_password,
        smtp_ssl=settings.smtp_use_tls,
        smtp_starttls=settings.smtp_starttls,
        production=settings.app_env == "production",
    )


def _create_email_challenge(request: Request, db: Session, email: str, purpose: str,
                            principal: Principal | None = None):
    _throttle(request, db, "email-send", email)
    settings = request.app.state.settings
    now, challenge_id = utcnow(), new_id()
    # A fixed code makes a walkthrough repeatable when mail only reaches the local
    # spool. It is worthless as authentication -- anyone who knows it can claim any
    # mailbox -- so Settings refuses it outside development.
    code = settings.demo_fixed_otp or f"{secrets.randbelow(1_000_000):06d}"
    # A resend replaces active challenges of the same purpose; it never extends
    # an existing code's validity.
    db.execute(update(EmailChallenge).where(
        EmailChallenge.email == email, EmailChallenge.purpose == purpose,
        EmailChallenge.consumed_at.is_(None),
    ).values(consumed_at=now))
    row = EmailChallenge(
        id=challenge_id, email=email, purpose=purpose,
        code_hash=digest(settings.secret_key, f"otp:{challenge_id}", code), attempts=0,
        expires_at=now + timedelta(seconds=settings.otp_ttl_seconds),
        request_ip_hash=_source(request),
        account_id=principal.account.id if principal else None,
        session_id=principal.session.id if principal else None,
    )
    db.add(row)
    db.commit()
    try:
        send_otp(_mail_config(request), email, code, expires_minutes=math.ceil(settings.otp_ttl_seconds / 60))
    except MailDeliveryError:
        row.consumed_at = utcnow()
        audit(db, request, actor_id=principal.account.id if principal else None,
              action="auth.email_delivery_failed", resource_id=row.id)
        db.commit()
        raise ApiError(503, "MAIL_UNAVAILABLE", "驗證信暫時無法寄送，請稍後再試。") from None
    return ok(request, {"challenge_id": row.id, "expires_at": row.expires_at,
                        "resend_after": now + timedelta(seconds=settings.otp_resend_seconds), "code_length": 6}, status_code=202)


def _consume_email(request: Request, db: Session, data: VerifyEmailInput, *, purpose: str | None = None):
    _throttle(request, db, "email-verify", data.challenge_id)
    row = db.scalar(select(EmailChallenge).where(EmailChallenge.id == data.challenge_id).with_for_update())
    if not row or row.consumed_at is not None or (purpose and row.purpose != purpose):
        raise ApiError(401, "OTP_INVALID", "驗證碼無效或已使用。")
    if _aware(row.expires_at) <= utcnow():
        row.consumed_at = utcnow()
        db.commit()
        raise ApiError(410, "OTP_EXPIRED", "驗證碼已過期，請重新取得。")
    settings = request.app.state.settings
    if row.attempts >= settings.otp_max_attempts:
        raise ApiError(401, "OTP_INVALID", "驗證碼無效或已使用。")
    if not hmac.compare_digest(row.code_hash, digest(settings.secret_key, f"otp:{row.id}", data.code)):
        row.attempts += 1
        if row.attempts >= settings.otp_max_attempts:
            row.consumed_at = utcnow()
        db.commit()
        raise ApiError(401, "OTP_INVALID", "驗證碼不正確。")
    row.consumed_at = utcnow()
    return row


@router.post("/auth/email/challenges")
def email_challenge(data: EmailChallengeInput, request: Request, db: Session = Depends(get_db)):
    _origin(request)
    principal = None
    email = _email(str(data.email))
    if data.purpose == "reauth":
        principal = current_principal(request, db)
        if email != principal.account.email or principal.session.auth_method == "staff_mfa":
            raise ApiError(403, "REAUTH_METHOD_REQUIRED", "請使用目前帳號的原登入方式重新驗證。")
    return _create_email_challenge(request, db, email, data.purpose, principal)


@router.post("/auth/email/challenges/verify")
def verify_email(data: VerifyEmailInput, request: Request, db: Session = Depends(get_db)):
    _origin(request)
    # Determine session-bound reauthentication before consuming the challenge:
    # current_principal commits bookkeeping and must not prematurely consume it.
    with request.app.state.session_factory() as lookup_db:
        purpose = lookup_db.scalar(select(EmailChallenge.purpose).where(EmailChallenge.id == data.challenge_id))
    principal = current_principal(request, db) if purpose == "reauth" else None
    if purpose == "email_change":
        raise ApiError(401, "OTP_INVALID", "請使用變更信箱流程完成驗證。")
    row = _consume_email(request, db, data)
    if row.purpose == "reauth":
        if not principal or row.account_id != principal.account.id or row.session_id != principal.session.id:
            raise ApiError(401, "OTP_INVALID", "驗證碼無效。")
        principal.session.reauth_at = utcnow()
        audit(db, request, actor_id=principal.account.id, action="auth.reauthenticated", resource_id=principal.session.id)
        db.commit()
        return ok(request, {"reauthenticated": True, "reauth_at": principal.session.reauth_at})
    account = db.scalar(select(Account).where(Account.email == row.email).with_for_update())
    if account is None:
        account = Account(id=new_id(), email=row.email, email_verified_at=utcnow(), status="ACTIVE", role_version=1)
        db.add(account)
        db.flush()
        db.add(RoleGrant(id=new_id(), account_id=account.id, role="applicant", scope_type="GLOBAL", scope_id=None))
    _active_account(account)
    grants = _grants(db, account.id)
    if any(grant.role in STAFF_ROLES for grant in grants) and not any(grant.role == "applicant" for grant in grants):
        db.commit()
        raise ApiError(403, "STAFF_LOGIN_REQUIRED", "工作帳號請使用密碼與第二因素登入。")
    account.email_verified_at = account.email_verified_at or utcnow()
    return _login_result(db, request, account, "email")


@router.get("/me")
def me(request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    token = request.cookies.get(f"{request.app.state.settings.session_cookie_name}_csrf")
    if token and not hmac.compare_digest(principal.session.csrf_hash, digest(request.app.state.settings.secret_key, "csrf", token)):
        token = None
    return ok(request, _me(db, principal, token))


@router.post("/auth/logout")
def logout(request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    principal.session.revoked_at = utcnow()
    audit(db, request, actor_id=principal.account.id, action="auth.logout", resource_id=principal.session.id)
    db.commit()
    response = Response(status_code=204)
    _clear_cookies(response, request)
    return response


@router.post("/auth/staff/session")
def staff_login(data: StaffLoginInput, request: Request, db: Session = Depends(get_db)):
    _origin(request)
    identifier = _email(data.identifier)
    _throttle(request, db, "staff-login", identifier)
    account = db.scalar(select(Account).where(Account.email == identifier))
    encoded = account.password_hash if account and account.password_hash else _DUMMY_PASSWORD
    password_valid = verify_password(encoded, data.password)
    if not password_valid or not account or account.status != "ACTIVE" or not account.password_hash:
        raise ApiError(401, "CREDENTIALS_INVALID", "帳號或密碼不正確。")
    if not any(grant.role in STAFF_ROLES for grant in _grants(db, account.id)):
        raise ApiError(401, "CREDENTIALS_INVALID", "帳號或密碼不正確。")
    if not account.totp_secret_encrypted:
        raise ApiError(503, "MFA_NOT_CONFIGURED", "工作帳號尚未完成第二因素設定，請聯絡管理人員。")
    row = MfaChallenge(id=new_id(), account_id=account.id, attempts=0,
                       expires_at=utcnow() + timedelta(minutes=5))
    db.add(row)
    db.commit()
    return ok(request, {"mfa_challenge_id": row.id, "expires_at": row.expires_at})


@router.post("/auth/staff/mfa")
def staff_mfa(data: VerifyMfaInput, request: Request, db: Session = Depends(get_db)):
    _origin(request)
    _throttle(request, db, "staff-mfa", data.mfa_challenge_id)
    challenge = db.scalar(select(MfaChallenge).where(MfaChallenge.id == data.mfa_challenge_id).with_for_update())
    if not challenge or challenge.consumed_at is not None:
        raise ApiError(401, "MFA_INVALID", "第二因素驗證無效或已使用。")
    if _aware(challenge.expires_at) <= utcnow():
        raise ApiError(410, "MFA_EXPIRED", "第二因素驗證已過期，請重新登入。")
    if challenge.attempts >= 5:
        raise ApiError(401, "MFA_INVALID", "第二因素驗證無效或已使用。")
    account = _active_account(db.scalar(select(Account).where(Account.id == challenge.account_id).with_for_update()))
    if not any(grant.role in STAFF_ROLES for grant in _grants(db, account.id)):
        raise ApiError(401, "MFA_INVALID", "工作帳號目前無法登入。")
    settings = request.app.state.settings
    try:
        secret = decrypt_totp_secret(account.totp_secret_encrypted or "", settings.secret_key, settings.totp_encryption_key)
    except ValueError:
        raise ApiError(503, "MFA_UNAVAILABLE", "第二因素服務暫時無法使用，請聯絡管理人員。") from None
    matched_step = match_totp_step(secret, data.code, utcnow().timestamp(), account.last_totp_step)
    if matched_step is None:
        challenge.attempts += 1
        if challenge.attempts >= 5:
            challenge.consumed_at = utcnow()
        db.commit()
        raise ApiError(401, "MFA_INVALID", "第二因素驗證碼不正確或已使用。")
    account.last_totp_step = matched_step
    challenge.consumed_at = utcnow()
    return _login_result(db, request, account, "staff_mfa")


def _verify_line_token(request: Request, id_token: str) -> dict:
    """Verify against LINE's official endpoint, never locally trust decoded claims.

    https://developers.line.biz/en/reference/line-login/#verify-id-token
    """
    settings = request.app.state.settings
    if not settings.line_channel_id or not settings.line_provider_id:
        raise ApiError(503, "LINE_NOT_CONFIGURED", "LINE 帳號連結尚未設定，請使用信箱登入。")
    try:
        response = httpx.post(
            "https://api.line.me/oauth2/v2.1/verify",
            data={"id_token": id_token, "client_id": settings.line_channel_id},
            timeout=10, follow_redirects=False, trust_env=False,
        )
    except httpx.HTTPError:
        raise ApiError(503, "LINE_UNAVAILABLE", "LINE 驗證服務暫時無法使用。") from None
    if response.status_code in {400, 401, 403}:
        raise ApiError(401, "TOKEN_INVALID", "LINE 驗證已失效，請重新登入。")
    if response.status_code != 200:
        raise ApiError(503, "LINE_UNAVAILABLE", "LINE 驗證服務暫時無法使用。")
    try:
        claims = response.json()
        valid = (isinstance(claims, dict)
                 and claims.get("iss") == "https://access.line.me"
                 and claims.get("aud") == settings.line_channel_id
                 and isinstance(claims.get("sub"), str) and 0 < len(claims["sub"]) <= 100
                 and isinstance(claims.get("exp"), (int, float))
                 and claims["exp"] > utcnow().timestamp()
                 and isinstance(claims.get("iat"), (int, float))
                 and claims["iat"] <= utcnow().timestamp() + 60)
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ApiError(401, "TOKEN_INVALID", "LINE 驗證已失效或不屬於本服務。")
    return claims


@router.post("/auth/line/session")
def line_login(data: LineTokenInput, request: Request, db: Session = Depends(get_db)):
    _origin(request)
    _throttle(request, db, "line-login")
    claims = _verify_line_token(request, data.id_token)
    link = db.scalar(select(IdentityLink).where(
        IdentityLink.provider_id == request.app.state.settings.line_provider_id,
        IdentityLink.line_subject == claims["sub"], IdentityLink.revoked_at.is_(None),
    ))
    if not link:
        raise ApiError(409, "LINK_REQUIRED", "請先以信箱登入，再連結 LINE 帳號。")
    account = _active_account(db.get(Account, link.account_id))
    if not any(grant.role == "applicant" for grant in _grants(db, account.id)):
        raise ApiError(403, "STAFF_LOGIN_REQUIRED", "工作帳號請使用密碼與第二因素登入。")
    return _login_result(db, request, account, "line")


@router.post("/identity-links/line/challenges")
def line_challenge(request: Request, principal: Principal = Depends(require_roles("applicant")), db: Session = Depends(get_db)):
    _recent(principal, request)
    settings = request.app.state.settings
    if not settings.line_channel_id or not settings.line_provider_id:
        raise ApiError(503, "LINE_NOT_CONFIGURED", "LINE 帳號連結尚未設定。")
    _throttle(request, db, "line-link", principal.account.id)
    row = LineChallenge(id=new_id(), account_id=principal.account.id, session_id=principal.session.id,
                        expires_at=utcnow() + timedelta(minutes=5))
    db.add(row)
    db.commit()
    return ok(request, {"challenge_id": row.id, "expires_at": row.expires_at}, status_code=201)


@router.post("/identity-links/line")
def bind_line(data: BindLineInput, request: Request, principal: Principal = Depends(require_roles("applicant")), db: Session = Depends(get_db)):
    _recent(principal, request)
    _throttle(request, db, "line-bind", principal.account.id)
    claims = _verify_line_token(request, data.id_token)
    row = db.scalar(select(LineChallenge).where(LineChallenge.id == data.challenge_id).with_for_update())
    if not row or row.consumed_at is not None or row.account_id != principal.account.id or row.session_id != principal.session.id:
        raise ApiError(401, "CHALLENGE_INVALID", "帳號連結驗證無效。")
    if _aware(row.expires_at) <= utcnow():
        raise ApiError(410, "CHALLENGE_EXPIRED", "帳號連結驗證已過期。")
    provider = request.app.state.settings.line_provider_id
    # Serialize links for one service account. A fresh link must not reuse an old
    # binding version, otherwise a queued notification could target its successor.
    db.scalar(select(Account).where(Account.id == principal.account.id).with_for_update())
    existing = db.scalar(select(IdentityLink).where(
        IdentityLink.revoked_at.is_(None),
        or_(IdentityLink.account_id == principal.account.id,
            (IdentityLink.provider_id == provider) & (IdentityLink.line_subject == claims["sub"])),
    ))
    if existing:
        raise ApiError(409, "LINK_CONFLICT", "LINE 或服務帳號已有連結，請先確認目前登入的帳號。")
    row.consumed_at = utcnow()
    previous_version = db.scalar(select(func.max(IdentityLink.binding_version)).where(
        IdentityLink.account_id == principal.account.id
    )) or 0
    link = IdentityLink(id=new_id(), account_id=principal.account.id, provider_id=provider,
                        line_subject=claims["sub"], binding_version=previous_version + 1, observed_follow_status="UNKNOWN")
    db.add(link)
    audit(db, request, actor_id=principal.account.id, action="identity.line_linked", resource_id=link.id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "LINK_CONFLICT", "LINE 或服務帳號已有連結，請重新確認。") from None
    return ok(request, {"link_id": link.id, "binding_version": link.binding_version, "version": link.version},
              status_code=201, headers={"ETag": etag(link)})


def _revoke_sessions(db: Session, account_id: str) -> None:
    db.execute(update(AuthSession).where(
        AuthSession.account_id == account_id, AuthSession.revoked_at.is_(None)
    ).values(revoked_at=utcnow()))


@router.delete("/identity-links/line")
def unlink_line(request: Request, principal: Principal = Depends(require_roles("applicant")), db: Session = Depends(get_db)):
    _recent(principal, request)
    link = db.scalar(select(IdentityLink).where(
        IdentityLink.account_id == principal.account.id, IdentityLink.revoked_at.is_(None)
    ).with_for_update())
    if not link:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "找不到可操作的帳號連結。")
    check_version(link, request.headers.get("if-match"))
    link.revoked_at = utcnow()
    link.binding_version += 1
    _revoke_sessions(db, principal.account.id)
    audit(db, request, actor_id=principal.account.id, action="identity.line_unlinked", resource_id=link.id)
    db.commit()
    response = Response(status_code=204)
    _clear_cookies(response, request)
    return response


@router.get("/account/sessions")
def account_sessions(request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    rows = list(db.scalars(select(AuthSession).where(
        AuthSession.account_id == principal.account.id, AuthSession.revoked_at.is_(None),
        AuthSession.expires_at > utcnow(),
    ).order_by(AuthSession.created_at.desc()).limit(100)))
    return ok(request, [{"id": row.id, "created_at": row.created_at, "last_seen_at": row.last_seen_at,
                         "expires_at": row.expires_at, "auth_method": row.auth_method,
                         "is_current": row.id == principal.session.id, "etag": etag(row)} for row in rows])


@router.delete("/account/sessions/{session_id}")
def revoke_session(session_id: str, request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    row = db.scalar(select(AuthSession).where(AuthSession.id == session_id, AuthSession.account_id == principal.account.id))
    if not row:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "找不到可操作的工作階段。")
    check_version(row, request.headers.get("if-match"))
    row.revoked_at = utcnow()
    audit(db, request, actor_id=principal.account.id, action="auth.session_revoked", resource_id=row.id)
    db.commit()
    response = Response(status_code=204)
    if row.id == principal.session.id:
        _clear_cookies(response, request)
    return response


@router.post("/account/sessions/revoke-all")
def revoke_all_sessions(request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    _recent(principal, request)
    _revoke_sessions(db, principal.account.id)
    audit(db, request, actor_id=principal.account.id, action="auth.all_sessions_revoked")
    db.commit()
    response = Response(status_code=204)
    _clear_cookies(response, request)
    return response


@router.post("/account/email/change-challenges")
def change_email_challenge(data: ChangeEmailInput, request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    _recent(principal, request)
    email = _email(str(data.email))
    if email == principal.account.email:
        raise ApiError(422, "EMAIL_UNCHANGED", "請輸入新的聯絡信箱。")
    # Do not reveal whether the proposed address belongs to an existing account.
    return _create_email_challenge(request, db, email, "email_change", principal)


@router.post("/account/email/change")
def change_email(data: VerifyEmailInput, request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    _recent(principal, request)
    row = _consume_email(request, db, data, purpose="email_change")
    if row.account_id != principal.account.id or row.session_id != principal.session.id:
        raise ApiError(401, "OTP_INVALID", "驗證碼無效。")
    duplicate = db.scalar(select(Account).where(Account.email == row.email, Account.id != principal.account.id))
    if duplicate:
        db.commit()
        raise ApiError(409, "EMAIL_UNAVAILABLE", "此信箱無法用於變更，請聯絡服務窗口。")
    principal.account.email = row.email
    principal.account.email_verified_at = utcnow()
    _revoke_sessions(db, principal.account.id)
    audit(db, request, actor_id=principal.account.id, action="account.email_changed", resource_id=principal.account.id)
    try:
        return _login_result(db, request, principal.account, principal.session.auth_method)
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "EMAIL_UNAVAILABLE", "此信箱無法用於變更，請聯絡服務窗口。") from None
