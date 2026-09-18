"""Private file upload, completion, and per-request authorized streaming."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.access import case_for
from app.auth import Principal, current_principal
from app.common import ApiError, audit, ok, utcnow
from app.db import get_db, new_id
from app.models import Case, CaseAccess, File, FileVersion, Scheme, Task
from app.storage import ALLOWED_TYPES, StorageError, inspect_object, private_path, store_once


router = APIRouter(tags=["files"])


class UploadIntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    case_id: str = Field(min_length=1, max_length=100)
    task_id: str | None = Field(default=None, min_length=1, max_length=100)
    document_type: str = Field(default="OTHER", min_length=1, max_length=100)
    file_name: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=1, strict=True)
    content_type: str = Field(min_length=1, max_length=100)

    @field_validator("file_name")
    @classmethod
    def valid_name(cls, name: str) -> str:
        if any(ord(char) < 32 or ord(char) == 127 for char in name) or "/" in name or "\\" in name:
            raise ValueError("檔名不可包含路徑或控制字元。")
        return name


class CompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_version_id: str = Field(min_length=1, max_length=100)


def _token_digest(token: str, settings) -> str:
    return hmac.new(settings.secret_key.encode(), b"file-upload:" + token.encode(), hashlib.sha256).hexdigest()


def _upload_case(db: Session, principal: Principal, case_id: str, task_id: str | None):
    case = case_for(db, principal, case_id, write=True)
    now = utcnow()
    ownership = db.scalar(select(CaseAccess.id).where(
        CaseAccess.case_id == case.id,
        CaseAccess.account_id == principal.account.id,
        CaseAccess.permission == "OWNER",
        CaseAccess.revoked_at.is_(None),
        or_(CaseAccess.expires_at.is_(None), CaseAccess.expires_at > now),
    ))
    if not ownership:
        raise ApiError(403, "UPLOAD_NOT_ALLOWED", "僅申請人可上傳申請文件。")
    if case.status not in {"DRAFT", "RECEIVED", "UNDER_REVIEW"}:
        raise ApiError(409, "CASE_NOT_EDITABLE", "目前案件不可上傳文件。")
    if task_id:
        task = db.get(Task, task_id)
        if not task or task.case_id != case.id:
            raise ApiError(404, "NOT_FOUND", "找不到可操作的任務。")
        if task.status not in {"OPEN", "REOPENED"}:
            raise ApiError(409, "TASK_NOT_EDITABLE", "目前任務不可上傳新補件。")
    elif case.status != "DRAFT":
        raise ApiError(409, "TASK_REQUIRED", "已送件案件的新文件須屬於有效補件任務。")
    return case


def _file_for(db, principal, file_id, file_version_id=None):
    file = db.get(File, file_id)
    if not file:
        raise ApiError(404, "NOT_FOUND", "找不到可存取的文件。")
    case_for(db, principal, file.case_id)
    version = db.get(FileVersion, file_version_id or file.current_version_id)
    if not version or version.file_id != file.id:
        raise ApiError(404, "NOT_FOUND", "找不到可存取的文件版本。")
    return file, version


def file_metadata(file, version):
    return {
        "file_id": file.id,
        "file_version_id": version.id,
        "case_id": file.case_id,
        "task_id": file.task_id,
        "document_type": file.document_type,
        "file_name": file.original_name,
        "size_bytes": version.size_bytes,
        "content_type": version.detected_type or version.declared_type,
        "scan_status": version.scan_status,
        "scan_engine": version.scan_engine_version,
        "uploaded_at": version.uploaded_at,
        "scanned_at": version.scanned_at,
        "sha256": version.sha256,
        "allowed_actions": ["download_file", "preview_file"] if version.scan_status == "CLEAN" else [],
    }


@router.post("/files/upload-intents", status_code=201)
def upload_intent(body: UploadIntentRequest, request: Request,
                  principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    settings = request.app.state.settings
    if body.size_bytes > settings.max_file_bytes:
        raise ApiError(413, "FILE_TOO_LARGE", "每個文件不得超過 20 MiB。")
    if body.content_type not in ALLOWED_TYPES:
        raise ApiError(415, "FILE_TYPE_NOT_ALLOWED", "僅接受 PDF、JPEG 或 PNG 文件。")
    case = _upload_case(db, principal, body.case_id, body.task_id)
    # Serialize reservations on PostgreSQL; size reservations count even before
    # upload, so concurrent intents cannot each spend the same case quota.
    db.execute(select(Case.id).where(Case.id == case.id).with_for_update()).all()
    scheme = db.get(Scheme, case.scheme_id)
    configured = (scheme.config or {}).get("document_types", ["OTHER"])
    allowed = set(configured.keys()) if isinstance(configured, dict) else set(configured)
    if body.document_type not in allowed:
        raise ApiError(422, "DOCUMENT_TYPE_NOT_ALLOWED", "文件類別不在本方案允許清單。")
    now = utcnow()
    reserved = db.scalar(select(func.coalesce(func.sum(FileVersion.size_bytes), 0)).join(
        File, FileVersion.file_id == File.id
    ).where(
        File.case_id == case.id,
        FileVersion.scan_status != "EXPIRED",
        or_(FileVersion.uploaded_at.is_not(None), FileVersion.upload_expires_at > now),
    )) or 0
    quota = int((scheme.config or {}).get("case_storage_quota_bytes", settings.case_storage_quota_bytes))
    if reserved + body.size_bytes > quota:
        raise ApiError(413, "CASE_STORAGE_LIMIT", "案件文件累計容量已達上限，請聯絡承辦。")
    token = secrets.token_urlsafe(32)
    file = File(id=new_id(), case_id=case.id, task_id=body.task_id,
                created_by=principal.account.id, document_type=body.document_type,
                original_name=body.file_name)
    version = FileVersion(id=new_id(), file_id=file.id, object_key=uuid4().hex,
                          size_bytes=body.size_bytes, declared_type=body.content_type,
                          scan_status="PENDING_UPLOAD", upload_token_digest=_token_digest(token, settings),
                          upload_expires_at=now + timedelta(seconds=settings.upload_ttl_seconds))
    file.current_version_id = version.id
    db.add(file)
    db.flush()
    db.add(version)
    audit(db, request, actor_id=principal.account.id, action="FILE_UPLOAD_INTENT",
          resource_id=file.id, case_id=case.id)
    db.commit()
    return ok(request, {
        "file_id": file.id, "file_version_id": version.id,
        "upload_url": f"/api/v1/files/{file.id}/content",
        "upload_method": "PUT", "upload_headers": {"X-Upload-Token": token, "Content-Type": body.content_type},
        "expires_at": version.upload_expires_at,
    }, status_code=201)


@router.put("/files/{file_id}/content")
async def upload_content(file_id: str, request: Request, x_upload_token: str = Header(default=""),
                         principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    file, version = _file_for(db, principal, file_id)
    _upload_case(db, principal, file.case_id, file.task_id)
    if file.created_by != principal.account.id:
        raise ApiError(404, "NOT_FOUND", "找不到可操作的上傳。")
    settings = request.app.state.settings
    if not x_upload_token or len(x_upload_token) > 200 or not version.upload_token_digest or not hmac.compare_digest(
        version.upload_token_digest, _token_digest(x_upload_token, settings)
    ):
        raise ApiError(403, "INVALID_UPLOAD_TOKEN", "上傳授權無效。")
    if version.uploaded_at or version.scan_status != "PENDING_UPLOAD":
        raise ApiError(409, "UPLOAD_ALREADY_USED", "本次上傳已完成，不可覆寫文件。")
    if version.upload_expires_at <= utcnow():
        raise ApiError(410, "UPLOAD_EXPIRED", "上傳授權已逾時，請重新取得。")
    try:
        size, digest, detected = await store_once(settings.storage_dir, version.object_key,
                                                  request.stream(), version.size_bytes, version.declared_type)
    except StorageError as exc:
        status = 413 if exc.code == "FILE_TOO_LARGE" else 415 if exc.code == "FILE_TYPE_MISMATCH" else 409
        raise ApiError(status, exc.code, "文件上傳未完成或格式不符，請確認後重試。") from exc
    version.sha256, version.detected_type = digest, detected
    db.commit()
    return ok(request, {"file_id": file.id, "file_version_id": version.id, "size_bytes": size,
                        "persisted": True, "scan_status": "PENDING_UPLOAD", "next_action": "complete_upload"})


@router.post("/files/{file_id}/complete")
def complete_upload(file_id: str, body: CompleteRequest, request: Request,
                    principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    file, version = _file_for(db, principal, file_id, body.file_version_id)
    if file.created_by != principal.account.id:
        raise ApiError(404, "NOT_FOUND", "找不到可操作的上傳。")
    # Completion remains idempotent after formal submission changed case state.
    if version.uploaded_at:
        return ok(request, file_metadata(file, version))
    _upload_case(db, principal, file.case_id, file.task_id)
    if version.upload_expires_at <= utcnow():
        raise ApiError(410, "UPLOAD_EXPIRED", "尚未完成確認的上傳授權已逾時。")
    try:
        size, digest, detected = inspect_object(request.app.state.settings.storage_dir, version.object_key)
    except StorageError as exc:
        raise ApiError(409, exc.code, "尚未收到完整文件，請重新上傳。") from exc
    if size != version.size_bytes or detected != version.declared_type:
        raise ApiError(422, "FILE_CONTENT_MISMATCH", "文件內容與上傳資料不符。")
    if version.sha256 and version.sha256 != digest:
        raise ApiError(409, "FILE_INTEGRITY_ERROR", "文件完整性驗證失敗。")
    version.sha256, version.detected_type = digest, detected
    version.uploaded_at, version.scan_status = utcnow(), "PENDING_SCAN"
    version.upload_token_digest = None
    audit(db, request, actor_id=principal.account.id, action="FILE_UPLOAD_COMPLETED",
          resource_id=file.id, case_id=file.case_id)
    db.commit()
    return ok(request, file_metadata(file, version))


@router.get("/files/{file_id}")
def get_file(file_id: str, request: Request, file_version_id: str | None = Query(default=None),
             principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    file, version = _file_for(db, principal, file_id, file_version_id)
    return ok(request, file_metadata(file, version))


def _download(file_id, request, file_version_id, principal, db, preview=False):
    file, version = _file_for(db, principal, file_id, file_version_id)
    if version.scan_status != "CLEAN":
        raise ApiError(409, "FILE_UNAVAILABLE", "文件尚未通過安全檢查，暫時不可閱覽。")
    if "range" in request.headers:
        raise ApiError(416, "RANGE_NOT_SUPPORTED", "此版本不支援分段下載，請重新取得完整文件。")
    path = private_path(request.app.state.settings.storage_dir, version.object_key)
    try:
        source = path.open("rb")
    except OSError as exc:
        raise ApiError(409, "FILE_UNAVAILABLE", "文件暫時無法取得，請聯絡承辦。") from exc
    audit(db, request, actor_id=principal.account.id,
          action="FILE_PREVIEW" if preview else "FILE_DOWNLOAD", resource_id=version.id, case_id=file.case_id)
    try:
        db.commit()
    except Exception:
        source.close()
        raise

    def stream():
        try:
            while chunk := source.read(64 * 1024):
                yield chunk
        finally:
            source.close()

    # PDF is always an attachment: no same-origin execution of PDF active
    # content. Raster previews may render inline with sandbox CSP.
    disposition = "inline" if preview and version.detected_type in {"image/jpeg", "image/png"} else "attachment"
    return StreamingResponse(stream(), media_type=version.detected_type or "application/octet-stream", headers={
        "Content-Disposition": f"{disposition}; filename=\"document\"; filename*=UTF-8''{quote(file.original_name, safe='')}",
        "Content-Length": str(version.size_bytes), "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff", "Accept-Ranges": "none",
        "Content-Security-Policy": "sandbox; default-src 'none'",
    })


@router.get("/files/{file_id}/download")
def download(file_id: str, request: Request, file_version_id: str | None = Query(default=None),
             principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return _download(file_id, request, file_version_id, principal, db)


@router.get("/files/{file_id}/preview")
def preview(file_id: str, request: Request, file_version_id: str | None = Query(default=None),
            principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return _download(file_id, request, file_version_id, principal, db, preview=True)
