"""Administration flows with real cookies, CSRF and object-level authorization."""

import csv
from datetime import timedelta
from email import policy
from email.parser import BytesParser
import io
import re
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app.admin import _csv_cell, process_exports
from app.auth_crypto import encrypt_totp_secret, hash_password, totp_at
from app.common import etag
from app.config import Settings
from app.db import new_id, utcnow
from app.main import create_app
from app.models import Account, AuthSession, Case, CaseAccess, ClaimRequest, ExportJob, RoleGrant, Scheme


@pytest.fixture
def admin_env(tmp_path):
    settings = Settings(
        app_env="test", secret_key="admin-integration-secret-" * 3,
        totp_encryption_key=Fernet.generate_key().decode(),
        database_url=f"sqlite:///{tmp_path / 'admin.db'}", cookie_secure=True,
        allowed_origins=["https://service.test"], public_origin="https://service.test",
        mail_spool_dir=tmp_path / "mail", storage_dir=tmp_path / "files",
    )
    app = create_app(settings)
    factory = app.state.session_factory
    password = "correct horse battery staple"
    secret = "JBSWY3DPEHPK3PXP"
    encoded_password = hash_password(password)
    accounts = {}
    with factory() as db:
        for name in ("owner", "claimant", "admin", "supervisor", "other_supervisor", "reviewer", "auditor"):
            staff = name not in {"owner", "claimant"}
            account = Account(id=new_id(), email=f"{name}@example.org", email_verified_at=utcnow(),
                              password_hash=encoded_password if staff else None,
                              totp_secret_encrypted=encrypt_totp_secret(secret, settings.secret_key, settings.totp_encryption_key) if staff else None)
            db.add(account)
            accounts[name] = account
        scheme = Scheme(id=new_id(), name="方案 A")
        other_scheme = Scheme(id=new_id(), name="方案 B")
        db.add_all([scheme, other_scheme])
        db.flush()
        for name in ("owner", "claimant"):
            db.add(RoleGrant(account_id=accounts[name].id, role="applicant", scope_type="GLOBAL"))
        db.add(RoleGrant(account_id=accounts["admin"].id, role="admin", scope_type="GLOBAL"))
        for name, role, scope in (("supervisor", "supervisor", scheme.id),
                                  ("other_supervisor", "supervisor", other_scheme.id),
                                  ("reviewer", "reviewer", scheme.id), ("auditor", "auditor", scheme.id)):
            db.add(RoleGrant(account_id=accounts[name].id, role=role, scope_type="SCHEME", scope_id=scope))
        case = Case(id=new_id(), scheme_id=scheme.id, case_no="=2+3", status="RECEIVED",
                    created_by=accounts["owner"].id, assigned_to=accounts["reviewer"].id)
        db.add(case)
        db.flush()
        db.add(CaseAccess(case_id=case.id, account_id=accounts["owner"].id, permission="OWNER"))
        db.add(CaseAccess(case_id=case.id, account_id=accounts["auditor"].id, permission="READ"))
        db.commit()
        ids = {name: account.id for name, account in accounts.items()}
        case_id, scheme_id = case.id, scheme.id
    with TestClient(app, base_url="https://service.test", headers={"Origin": "https://service.test"}) as client:
        yield SimpleNamespace(app=app, client=client, settings=settings, factory=factory,
                              ids=ids, case_id=case_id, scheme_id=scheme_id,
                              password=password, secret=secret)


def _staff_login(env, name="auditor", client=None):
    client = client or env.client
    password = client.post("/api/v1/auth/staff/session", json={"identifier": f"{name}@example.org", "password": env.password})
    assert password.status_code == 200, password.text
    response = client.post("/api/v1/auth/staff/mfa", json={
        "mfa_challenge_id": password.json()["data"]["mfa_challenge_id"],
        "code": totp_at(env.secret, int(utcnow().timestamp() // 30)),
    })
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["data"]["csrf_token"]


def _youth_login(env, name="claimant", client=None):
    client = client or env.client
    response = client.post("/api/v1/auth/email/challenges", json={"email": f"{name}@example.org"})
    assert response.status_code == 202, response.text
    path = max(env.settings.mail_spool_dir.glob("*.eml"), key=lambda item: item.stat().st_mtime_ns)
    message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    code = re.search(r"驗證碼：([0-9]{6})", message.get_body(preferencelist=("plain",)).get_content()).group(1)
    verified = client.post("/api/v1/auth/email/challenges/verify", json={"challenge_id": response.json()["data"]["challenge_id"], "code": code})
    assert verified.status_code == 200, verified.text
    client.headers["X-CSRF-Token"] = verified.json()["data"]["csrf_token"]


def _request_export(env):
    response = env.client.post("/api/v1/staff/exports", json={
        "scheme_id": env.scheme_id, "columns": ["case_no", "status"], "purpose": "例行業務統計使用",
    })
    assert response.status_code == 202, response.text
    return response.json()["data"]["id"]


def _claim(env):
    response = env.client.post("/api/v1/case-claims", json={"reference": "=2+3", "contact_note": "請依既有驗身程序辦理"})
    assert response.status_code == 202, response.text
    return response.json()["data"]["id"], response.headers["etag"]


def _resolution(env):
    return {"action": "APPROVE", "case_id": env.case_id,
            "verification_id": "manual-verification-2026-00001", "verification_confirmed": True,
            "public_message": "已依機關既有程序完成確認。"}


def test_export_create_worker_download_uses_real_authorization(admin_env):
    _staff_login(admin_env)
    csrf = admin_env.client.headers.pop("X-CSRF-Token")
    denied = admin_env.client.post("/api/v1/staff/exports", json={"scheme_id": admin_env.scheme_id, "purpose": "例行業務統計使用"})
    assert denied.status_code == 403
    admin_env.client.headers["X-CSRF-Token"] = csrf
    job_id = _request_export(admin_env)
    assert admin_env.client.get(f"/api/v1/staff/exports/{job_id}/download").status_code == 409
    assert process_exports(admin_env.factory, admin_env.settings) == 1
    result = admin_env.client.get(f"/api/v1/staff/exports/{job_id}").json()["data"]
    assert result["status"] == "READY"
    response = admin_env.client.get(result["download_path"])
    assert response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows == [["case_no", "status"], ["'=2+3", "RECEIVED"]]


def test_ready_export_rechecks_case_access_for_every_download(admin_env):
    _staff_login(admin_env)
    job_id = _request_export(admin_env)
    assert process_exports(admin_env.factory, admin_env.settings) == 1
    with admin_env.factory() as db:
        access = db.scalar(select(CaseAccess).where(CaseAccess.account_id == admin_env.ids["auditor"]))
        access.revoked_at = utcnow()
        db.commit()
    response = admin_env.client.get(f"/api/v1/staff/exports/{job_id}/download", headers={"Range": "bytes=0-10"})
    assert response.status_code == 403 and response.json()["error"]["code"] == "EXPORT_ACCESS_REVOKED"


def test_pending_export_is_cancelled_when_access_is_revoked(admin_env):
    _staff_login(admin_env)
    job_id = _request_export(admin_env)
    with admin_env.factory() as db:
        db.scalar(select(CaseAccess).where(CaseAccess.account_id == admin_env.ids["auditor"])).revoked_at = utcnow()
        db.commit()
    assert process_exports(admin_env.factory, admin_env.settings) == 0
    assert admin_env.client.get(f"/api/v1/staff/exports/{job_id}").json()["data"]["status"] == "CANCELLED"


def test_export_is_private_to_requester_and_expires(admin_env):
    _staff_login(admin_env)
    job_id = _request_export(admin_env)
    process_exports(admin_env.factory, admin_env.settings)
    with TestClient(admin_env.app, base_url="https://service.test", headers={"Origin": "https://service.test"}) as supervisor:
        _staff_login(admin_env, "supervisor", supervisor)
        assert supervisor.get(f"/api/v1/staff/exports/{job_id}/download").status_code == 404
    with admin_env.factory() as db:
        db.get(ExportJob, job_id).expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert admin_env.client.get(f"/api/v1/staff/exports/{job_id}/download").status_code == 410


@pytest.mark.parametrize("value", ["=SUM(1,1)", "+2+3", "-2+3", "@SUM(A1)", "  =2+3", "\tdata", "\rdata", "\ndata"])
def test_csv_formula_and_control_prefixes_are_neutralized(value):
    assert _csv_cell(value).startswith("'")
    assert _csv_cell(0) == "0"


def test_admin_has_no_implicit_case_or_export_authority(admin_env):
    _staff_login(admin_env, "admin")
    assert admin_env.client.get(f"/api/v1/staff/cases/{admin_env.case_id}").status_code in {403, 404}
    assert admin_env.client.post("/api/v1/staff/exports", json={"scheme_id": admin_env.scheme_id, "purpose": "例行業務統計使用"}).status_code == 403
    accounts = admin_env.client.get("/api/v1/admin/accounts")
    assert accounts.status_code == 200
    assert "password_hash" not in accounts.text and "totp_secret" not in accounts.text


def test_admin_account_disable_revokes_existing_session(admin_env):
    _staff_login(admin_env, "admin")
    with TestClient(admin_env.app, base_url="https://service.test", headers={"Origin": "https://service.test"}) as youth:
        _youth_login(admin_env, "owner", youth)
        with admin_env.factory() as db:
            version = etag(db.get(Account, admin_env.ids["owner"]))
        response = admin_env.client.patch(f"/api/v1/admin/accounts/{admin_env.ids['owner']}/status",
                                         headers={"If-Match": version}, json={"status": "DISABLED", "reason": "依核定程序停用帳號"})
        assert response.status_code == 200, response.text
        assert youth.get("/api/v1/me").status_code == 401
        with admin_env.factory() as db:
            assert all(row.revoked_at is not None for row in db.scalars(select(AuthSession).where(AuthSession.account_id == admin_env.ids["owner"])))


def test_knowing_case_reference_does_not_grant_ownership(admin_env):
    _youth_login(admin_env)
    claim_id, _ = _claim(admin_env)
    assert admin_env.client.get(f"/api/v1/cases/{admin_env.case_id}").status_code == 404
    result = admin_env.client.get(f"/api/v1/case-claims/{claim_id}").json()["data"]
    assert result["status"] == "PENDING" and "reference" not in result
    with admin_env.factory() as db:
        assert db.get(ClaimRequest, claim_id).reference != "=2+3"
        assert db.scalar(select(func.count()).select_from(CaseAccess).where(CaseAccess.account_id == admin_env.ids["claimant"])) == 0


def test_claim_resolution_requires_supervisor_and_case_scope(admin_env):
    _youth_login(admin_env)
    claim_id, version = _claim(admin_env)
    for name, expected in (("reviewer", 403), ("other_supervisor", 404)):
        with TestClient(admin_env.app, base_url="https://service.test", headers={"Origin": "https://service.test"}) as staff:
            _staff_login(admin_env, name, staff)
            response = staff.post(f"/api/v1/staff/case-claims/{claim_id}/resolve", headers={"If-Match": version}, json=_resolution(admin_env))
            assert response.status_code == expected, response.text


def test_claim_resolution_records_manual_verification_before_granting_access(admin_env):
    _youth_login(admin_env)
    claim_id, version = _claim(admin_env)
    with TestClient(admin_env.app, base_url="https://service.test", headers={"Origin": "https://service.test"}) as staff:
        _staff_login(admin_env, "supervisor", staff)
        body = _resolution(admin_env)
        del body["verification_confirmed"]
        assert staff.post(f"/api/v1/staff/case-claims/{claim_id}/resolve", headers={"If-Match": version}, json=body).status_code == 422
        response = staff.post(f"/api/v1/staff/case-claims/{claim_id}/resolve", headers={"If-Match": version}, json=_resolution(admin_env))
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "APPROVED"
    assert admin_env.client.get(f"/api/v1/cases/{admin_env.case_id}").status_code == 200
    with admin_env.factory() as db:
        access = db.scalar(select(CaseAccess).where(CaseAccess.account_id == admin_env.ids["claimant"]))
        assert access.verification_id == "manual-verification-2026-00001"


def test_admin_cannot_self_approve_new_high_privilege_grants(admin_env):
    _staff_login(admin_env, "admin")
    with admin_env.factory() as db:
        version = etag(db.get(Account, admin_env.ids["reviewer"]))
    for role in ("supervisor", "admin", "auditor", "reviewer"):
        response = admin_env.client.patch(f"/api/v1/admin/accounts/{admin_env.ids['reviewer']}/roles", headers={"If-Match": version}, json={
            "grants": [{"role": role, "scope_type": "GLOBAL"}], "reason": "測試高權限新增限制",
        })
        assert response.status_code == 409 and response.json()["error"]["code"] == "GRANT_APPROVAL_REQUIRED"


def test_existing_high_privilege_may_be_reduced_but_not_expanded(admin_env):
    _staff_login(admin_env, "admin")
    with admin_env.factory() as db:
        version = etag(db.get(Account, admin_env.ids["supervisor"]))
    reduced = admin_env.client.patch(f"/api/v1/admin/accounts/{admin_env.ids['supervisor']}/roles", headers={"If-Match": version}, json={
        "grants": [{"role": "supervisor", "scope_type": "CASE", "scope_id": admin_env.case_id}], "reason": "依核定縮小案件權限",
    })
    assert reduced.status_code == 200, reduced.text
    expanded = admin_env.client.patch(f"/api/v1/admin/accounts/{admin_env.ids['supervisor']}/roles", headers={"If-Match": reduced.headers["etag"]}, json={
        "grants": [{"role": "supervisor", "scope_type": "SCHEME", "scope_id": admin_env.scheme_id}], "reason": "測試禁止擴大授權範圍",
    })
    assert expanded.status_code == 409
    revoked = admin_env.client.patch(f"/api/v1/admin/accounts/{admin_env.ids['supervisor']}/roles", headers={"If-Match": reduced.headers["etag"]}, json={
        "grants": [], "reason": "依核定撤銷所有工作權限",
    })
    assert revoked.status_code == 200 and revoked.json()["data"]["grants"] == []
