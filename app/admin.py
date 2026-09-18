"""Bounded administration, verified claims, reports and revocable CSV exports."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
import io
import os
from pathlib import Path
from typing import Literal

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.access import case_filter, case_for
from app.auth import Principal, _grants, _recent, current_principal, require_roles
from app.common import ApiError, audit, check_version, etag, new_id, ok
from app.db import get_db, utcnow
from app.models import (Account, AuditEvent, AuthSession, Case, CaseAccess, ClaimRequest,
                        ContentVersion, ExportJob, RoleGrant, Scheme)

router = APIRouter(tags=["administration"])
EXPORT_COLUMNS = {"case_no", "scheme_id", "status", "created_at", "last_business_update_at"}


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ClaimInput(Input):
    reference: str = Field(min_length=1, max_length=100)
    contact_note: str | None = Field(default=None, max_length=1000)


class ResolveClaimInput(Input):
    action: Literal["APPROVE", "REJECT"]
    case_id: str = Field(min_length=1, max_length=64)
    verification_id: str = Field(min_length=1, max_length=200)
    verification_confirmed: Literal[True] = Field(
        description="主管確認已完成機關既有驗身程序；此勾選本身不等於身分驗證。"
    )
    public_message: str = Field(min_length=1, max_length=1000)


class ExportInput(Input):
    scheme_id: str = Field(min_length=1, max_length=64)
    status: str | None = Field(default=None, max_length=40)
    columns: list[str] = Field(default=["case_no", "status", "created_at"], min_length=1, max_length=5)
    purpose: str = Field(min_length=5, max_length=500)


class StatusInput(Input):
    status: Literal["ACTIVE", "DISABLED"]
    reason: str = Field(min_length=5, max_length=500)


class GrantInput(Input):
    role: Literal["applicant", "reviewer", "supervisor", "admin", "auditor"]
    scope_type: Literal["GLOBAL", "SCHEME", "CASE"]
    scope_id: str | None = Field(default=None, max_length=64)
    expires_at: datetime | None = None


class RolesInput(Input):
    grants: list[GrantInput] = Field(default_factory=list, max_length=20)
    reason: str = Field(min_length=5, max_length=500)


def _global_admin(principal):
    if not any(g.role == "admin" and g.scope_type == "GLOBAL" for g in principal.grants):
        raise ApiError(403, "ROLE_REQUIRED", "需要全域帳號管理權限。")


def _claim_data(claim):
    # Case references and verification evidence are never returned to claimants.
    return {"id": claim.id, "status": claim.status, "public_message": claim.public_message,
            "created_at": claim.created_at, "version": claim.version, "etag": etag(claim)}


@router.post("/case-claims", status_code=202)
def request_claim(body: ClaimInput, request: Request,
                  principal: Principal = Depends(require_roles("applicant")), db: Session = Depends(get_db)):
    _recent(principal, request)
    count = db.scalar(select(func.count()).select_from(ClaimRequest).where(
        ClaimRequest.requested_by == principal.account.id, ClaimRequest.status == "PENDING"))
    if count >= 5:
        raise ApiError(429, "TOO_MANY_CLAIMS", "待確認的認領申請已達上限。")
    cipher = Fernet(request.app.state.settings.totp_encryption_key.encode())
    claim = ClaimRequest(id=new_id(), requested_by=principal.account.id,
                         reference=cipher.encrypt(body.reference.encode()).decode(),
                         contact_note=cipher.encrypt(body.contact_note.encode()).decode() if body.contact_note else None)
    db.add(claim)
    audit(db, request, actor_id=principal.account.id, action="claim.requested", resource_id=claim.id)
    db.commit()
    return ok(request, _claim_data(claim), 202, {"ETag": etag(claim)})


@router.get("/case-claims/{claim_id}")
def get_claim(claim_id: str, request: Request, principal: Principal = Depends(current_principal),
              db: Session = Depends(get_db)):
    claim = db.scalar(select(ClaimRequest).where(ClaimRequest.id == claim_id,
                                               ClaimRequest.requested_by == principal.account.id))
    if claim is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "找不到此申請。")
    return ok(request, _claim_data(claim), headers={"ETag": etag(claim)})


@router.post("/staff/case-claims/{claim_id}/resolve")
def resolve_claim(claim_id: str, body: ResolveClaimInput, request: Request,
                  if_match: str | None = Header(default=None),
                  principal: Principal = Depends(require_roles("supervisor")), db: Session = Depends(get_db)):
    _recent(principal, request)
    case_for(db, principal, body.case_id, write=True)
    claim = db.scalar(select(ClaimRequest).where(ClaimRequest.id == claim_id).with_for_update())
    if claim is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "找不到此申請。")
    check_version(claim, if_match)
    if claim.status != "PENDING":
        raise ApiError(409, "CLAIM_RESOLVED", "此申請已完成審查。")
    claim.status = "APPROVED" if body.action == "APPROVE" else "REJECTED"
    claim.verification_id, claim.resolved_case_id = body.verification_id, body.case_id
    claim.resolved_by, claim.public_message = principal.account.id, body.public_message
    if body.action == "APPROVE":
        existing = db.scalar(select(CaseAccess).where(
            CaseAccess.case_id == body.case_id, CaseAccess.account_id == claim.requested_by,
            CaseAccess.revoked_at.is_(None), CaseAccess.permission == "OWNER",
        ))
        if existing is None or (existing.expires_at is not None and existing.expires_at <= utcnow()):
            db.add(CaseAccess(case_id=body.case_id, account_id=claim.requested_by, permission="OWNER",
                              grant_source="VERIFIED_PROCEDURE", verification_id=body.verification_id))
    audit(db, request, actor_id=principal.account.id, action="claim.resolved", resource_id=claim.id,
          case_id=body.case_id, details={"outcome": claim.status, "verification_id": body.verification_id,
                                        "verification_confirmed": body.verification_confirmed})
    db.commit()
    return ok(request, _claim_data(claim), headers={"ETag": etag(claim)})


def _export_data(job):
    return {"id": job.id, "status": job.status, "columns": job.column_set,
            "created_at": job.created_at, "expires_at": job.expires_at,
            "download_path": f"/api/v1/staff/exports/{job.id}/download" if job.status == "READY" else None}


@router.post("/exports", status_code=202)
@router.post("/staff/exports", status_code=202)
def request_export(body: ExportInput, request: Request,
                   principal: Principal = Depends(require_roles("reviewer", "supervisor", "auditor")),
                   db: Session = Depends(get_db)):
    _recent(principal, request)
    if len(body.columns) != len(set(body.columns)) or not set(body.columns) <= EXPORT_COLUMNS:
        raise ApiError(422, "EXPORT_COLUMNS_INVALID", "匯出欄位不在允許清單內。")
    active = db.scalar(select(func.count()).select_from(ExportJob).where(
        ExportJob.requested_by == principal.account.id, ExportJob.status.in_(["PENDING", "PROCESSING"])))
    if active >= 3:
        raise ApiError(429, "EXPORT_LIMIT", "已有匯出工作正在處理。")
    query = select(Case.id).where(case_filter(principal), Case.scheme_id == body.scheme_id)
    if body.status:
        query = query.where(Case.status == body.status)
    case_ids = list(db.scalars(query.limit(10001)))
    if not case_ids:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "沒有可匯出的案件。")
    if len(case_ids) > 10000:
        raise ApiError(422, "EXPORT_TOO_LARGE", "請縮小篩選條件至一萬件以下。")
    job = ExportJob(id=new_id(), requested_by=principal.account.id,
                    filter_snapshot={"scheme_id": body.scheme_id, "status": body.status},
                    column_set=body.columns, purpose=body.purpose, case_ids=case_ids,
                    expires_at=utcnow() + timedelta(seconds=request.app.state.settings.export_ttl_seconds))
    db.add(job)
    audit(db, request, actor_id=principal.account.id, action="export.requested", resource_id=job.id,
          details={"count": len(case_ids), "columns": body.columns, "purpose": body.purpose})
    db.commit()
    return ok(request, _export_data(job), 202)


def _owned_export(db, principal, job_id):
    job = db.scalar(select(ExportJob).where(ExportJob.id == job_id, ExportJob.requested_by == principal.account.id))
    if not job:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "找不到此匯出工作。")
    return job


@router.get("/exports/{job_id}")
@router.get("/staff/exports/{job_id}")
def get_export(job_id: str, request: Request,
               principal: Principal = Depends(require_roles("reviewer", "supervisor", "auditor")),
               db: Session = Depends(get_db)):
    job = _owned_export(db, principal, job_id)
    data = _export_data(job)
    if job.expires_at <= utcnow():
        data.update(status="EXPIRED", download_path=None)
    return ok(request, data)


def _can_export(db, principal, case_ids):
    allowed = set(db.scalars(select(Case.id).where(Case.id.in_(case_ids), case_filter(principal))))
    return allowed == set(case_ids)


@router.get("/exports/{job_id}/download")
@router.get("/staff/exports/{job_id}/download")
def download_export(job_id: str, request: Request,
                    principal: Principal = Depends(require_roles("reviewer", "supervisor", "auditor")),
                    db: Session = Depends(get_db)):
    _recent(principal, request)
    job = _owned_export(db, principal, job_id)
    if job.expires_at <= utcnow():
        raise ApiError(410, "EXPORT_EXPIRED", "匯出檔案已過期，請重新申請。")
    if job.status != "READY":
        raise ApiError(409, "EXPORT_NOT_READY", "匯出檔案尚未完成。")
    if not _can_export(db, principal, job.case_ids):
        raise ApiError(403, "EXPORT_ACCESS_REVOKED", "案件存取權已變更，請重新申請匯出。")
    path = _export_path(request.app.state.settings, job.id)
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        raise ApiError(410, "EXPORT_UNAVAILABLE", "匯出檔案已不可用。") from None
    audit(db, request, actor_id=principal.account.id, action="export.downloaded", resource_id=job.id)
    db.commit()
    return Response(content, media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="cases-{job.id}.csv"', "Cache-Control": "private, no-store"})


def _export_path(settings, job_id):
    # job_id is a server-issued UUID read from the database, never a client path.
    return Path(settings.storage_dir) / "exports" / f"{job_id}.csv"


def _csv_cell(value):
    value = value.isoformat() if isinstance(value, datetime) else ("" if value is None else str(value))
    if value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def process_exports(session_factory, settings):
    """Small bounded exports hold a row lock; crashes roll back and may safely retry."""
    processed = 0
    with session_factory() as db:
        jobs = list(db.scalars(select(ExportJob).where(ExportJob.status == "PENDING")
                               .order_by(ExportJob.created_at).limit(5).with_for_update(skip_locked=True)))
        for job in jobs:
            account = db.get(Account, job.requested_by)
            grants = _grants(db, account.id) if account and account.status == "ACTIVE" else []
            principal = Principal(account, None, grants, {g.role for g in grants})
            if (job.expires_at <= utcnow() or not principal.roles.intersection({"reviewer", "supervisor", "auditor"})
                    or not _can_export(db, principal, job.case_ids)):
                job.status = "CANCELLED"
                continue
            cases = list(db.scalars(select(Case).where(Case.id.in_(job.case_ids)).order_by(Case.case_no)))
            buffer = io.StringIO(newline="")
            writer = csv.writer(buffer)
            writer.writerow(job.column_set)
            writer.writerows([_csv_cell(getattr(case, column)) for column in job.column_set] for case in cases)
            path = _export_path(settings, job.id)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # This exact job-owned file may exist after a worker crash before DB commit.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(buffer.getvalue().encode("utf-8-sig"))
            job.object_key = f"exports/{job.id}.csv"
            job.status, job.completed_at = "READY", utcnow()
            processed += 1
        db.commit()
    return processed


@router.get("/reports/summary")
def reports(request: Request, scheme_id: str | None = None,
            principal: Principal = Depends(require_roles("reviewer", "supervisor", "auditor")),
            db: Session = Depends(get_db)):
    query = select(Case.status, func.count()).where(case_filter(principal))
    if scheme_id:
        query = query.where(Case.scheme_id == scheme_id)
    counts = dict(db.execute(query.group_by(Case.status)).all())
    return ok(request, {"total": sum(counts.values()), "by_status": counts, "as_of": utcnow()})


@router.get("/audit-events")
def audit_events(request: Request, case_id: str | None = None, limit: int = Query(50, ge=1, le=200),
                 principal: Principal = Depends(require_roles("reviewer", "supervisor", "auditor", "admin")),
                 db: Session = Depends(get_db)):
    if case_id:
        case_for(db, principal, case_id)
        query = select(AuditEvent).where(AuditEvent.case_id == case_id)
    elif "admin" in principal.roles:
        _global_admin(principal)
        query = select(AuditEvent).where(AuditEvent.case_id.is_(None))
    else:
        query = select(AuditEvent).where(AuditEvent.case_id.in_(select(Case.id).where(case_filter(principal))))
    rows = db.scalars(query.order_by(AuditEvent.occurred_at.desc()).limit(limit))
    return ok(request, {"items": [{"id": row.id, "action": row.action, "actor_id": row.actor_id,
                                    "resource_id": row.resource_id, "case_id": row.case_id,
                                    "occurred_at": row.occurred_at, "details": row.details} for row in rows]})


def _account_data(db, account):
    return {"id": account.id, "email": account.email, "status": account.status,
            "version": account.version, "etag": etag(account), "role_version": account.role_version,
            "grants": [{"id": g.id, "role": g.role, "scope_type": g.scope_type,
                        "scope_id": g.scope_id, "expires_at": g.expires_at} for g in _grants(db, account.id)]}


@router.get("/admin/accounts")
def accounts(request: Request, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
             principal: Principal = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    _global_admin(principal)
    rows = db.scalars(select(Account).order_by(Account.created_at, Account.id).offset(offset).limit(limit))
    return ok(request, {"items": [_account_data(db, row) for row in rows], "offset": offset, "limit": limit})


def _modifiable_account(db, principal, account_id, if_match):
    _global_admin(principal)
    if account_id == principal.account.id:
        raise ApiError(403, "SELF_MODIFICATION_FORBIDDEN", "不得以此端點變更自己的權限或狀態。")
    account = db.scalar(select(Account).where(Account.id == account_id).with_for_update())
    if account is None:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "找不到此帳號。")
    check_version(account, if_match)
    return account


def _revoke_sessions(db, account):
    account.role_version += 1
    for session in db.scalars(select(AuthSession).where(AuthSession.account_id == account.id,
                                                       AuthSession.revoked_at.is_(None))):
        session.revoked_at = utcnow()


@router.patch("/admin/accounts/{account_id}/status")
def change_status(account_id: str, body: StatusInput, request: Request,
                  if_match: str | None = Header(default=None),
                  principal: Principal = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    _recent(principal, request)
    account = _modifiable_account(db, principal, account_id, if_match)
    account.status = body.status
    _revoke_sessions(db, account)
    audit(db, request, actor_id=principal.account.id, action="account.status_changed", resource_id=account.id,
          details={"status": body.status, "reason": body.reason})
    db.commit()
    return ok(request, _account_data(db, account), headers={"ETag": etag(account)})


def _grant_within(db, proposed, existing):
    if existing.role != proposed.role:
        return False
    if existing.expires_at and (not proposed.expires_at or proposed.expires_at > existing.expires_at):
        return False
    if existing.scope_type == "GLOBAL":
        return True
    if existing.scope_type == proposed.scope_type and existing.scope_id == proposed.scope_id:
        return True
    if existing.scope_type == "SCHEME" and proposed.scope_type == "CASE":
        case = db.get(Case, proposed.scope_id)
        return case is not None and case.scheme_id == existing.scope_id
    return False


@router.put("/admin/accounts/{account_id}/roles")
@router.patch("/admin/accounts/{account_id}/roles")
def change_roles(account_id: str, body: RolesInput, request: Request,
                 if_match: str | None = Header(default=None),
                 principal: Principal = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    _recent(principal, request)
    account = _modifiable_account(db, principal, account_id, if_match)
    existing_grants = _grants(db, account.id)
    seen = set()
    for grant in body.grants:
        signature = (grant.role, grant.scope_type, grant.scope_id)
        if signature in seen:
            raise ApiError(422, "DUPLICATE_GRANT", "不得重複授權。")
        seen.add(signature)
        if grant.scope_type == "GLOBAL" and grant.scope_id is not None:
            raise ApiError(422, "INVALID_SCOPE", "全域授權不得指定範圍代碼。")
        if grant.scope_type in {"CASE", "SCHEME"} and (not grant.scope_id or not db.get(
                Case if grant.scope_type == "CASE" else Scheme, grant.scope_id)):
            raise ApiError(422, "INVALID_SCOPE", "授權範圍不存在。")
        if grant.role in {"applicant", "admin"} and grant.scope_type != "GLOBAL":
            raise ApiError(422, "INVALID_SCOPE", "此角色須使用全域授權。")
        if grant.expires_at and (grant.expires_at.tzinfo is None or grant.expires_at <= utcnow()):
            raise ApiError(422, "INVALID_EXPIRY", "請提供含時區的未來到期時間。")
        if grant.role != "applicant" and (not account.password_hash or not account.totp_secret_encrypted):
            raise ApiError(409, "MFA_ENROLLMENT_REQUIRED", "請先以管理命令完成承辦帳號與 MFA 設定。")
        protected = grant.role in {"supervisor", "admin", "auditor"} or (
            grant.role != "applicant" and grant.scope_type == "GLOBAL"
        )
        if protected and not any(_grant_within(db, grant, old) for old in existing_grants):
            raise ApiError(409, "GRANT_APPROVAL_REQUIRED", "新增或擴大高權限須先完成獨立人工核准，不可由此端點自行授予。")
    for old in existing_grants:
        old.revoked_at = utcnow()
    for grant in body.grants:
        db.add(RoleGrant(account_id=account.id, **grant.model_dump()))
    _revoke_sessions(db, account)
    audit(db, request, actor_id=principal.account.id, action="account.roles_changed", resource_id=account.id,
          details={"reason": body.reason, "grants": [g.model_dump(mode="json") for g in body.grants]})
    db.commit()
    return ok(request, _account_data(db, account), headers={"ETag": etag(account)})


@router.get("/security-tips")
def security_tips(request: Request, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    tips = db.scalars(select(ContentVersion).where(ContentVersion.kind == "TIP", ContentVersion.status == "PUBLISHED")
                      .order_by(ContentVersion.code, ContentVersion.version_no.desc()).limit(100))
    result = {}
    for tip in tips:
        result.setdefault(tip.code, {"id": tip.id, "code": tip.code, "version": tip.version_no, "content": tip.body})
    return ok(request, {"items": list(result.values())})
