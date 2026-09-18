from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import new_id, utcnow
from app.models import AuditEvent, IdempotencyRecord


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, field_errors=None):
        self.status = status
        self.code = code
        self.message = message
        self.field_errors = field_errors or []
        super().__init__(message)


def meta(request):
    return {"request_id": getattr(request.state, "request_id", new_id()),
            "server_time": utcnow().isoformat().replace("+00:00", "Z")}


def ok(request, data, status_code=200, headers=None):
    return JSONResponse(encode({"data": data, "meta": meta(request)}),
                        status_code=status_code, headers=headers)


def encode(value):
    return jsonable_encoder(value, custom_encoder={
        datetime: lambda value: value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")})


def etag(obj):
    return f'"{obj.__tablename__.removesuffix("s")}-{obj.id}-v{obj.version}"'


def check_version(obj, if_match):
    if not if_match:
        raise ApiError(428, "PRECONDITION_REQUIRED", "請提供 If-Match 資源版本。")
    if if_match != etag(obj):
        raise ApiError(412, "VERSION_CONFLICT", "資料已更新，請重新讀取後再操作。")


def audit(db, request, *, actor_id, action, resource_id=None, case_id=None, details=None):
    row = AuditEvent(actor_id=actor_id, action=action, resource_id=resource_id,
                     case_id=case_id, details=jsonable_encoder(details or {}),
                     request_id=getattr(request.state, "request_id", new_id()) if request else new_id())
    db.add(row)
    return row


def idem_start(db, principal, request, scope, payload):
    key = request.headers.get("Idempotency-Key", "")
    if not 8 <= len(key) <= 128 or not key.isascii():
        raise ApiError(428, "IDEMPOTENCY_REQUIRED", "請提供 8–128 字元的 Idempotency-Key。")
    digest = hashlib.sha256(json.dumps(jsonable_encoder(payload), sort_keys=True,
                                       separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    query = select(IdempotencyRecord).where(
        IdempotencyRecord.account_id == principal.account.id,
        IdempotencyRecord.method == request.method,
        IdempotencyRecord.route_scope == scope,
        IdempotencyRecord.key == key,
    )

    def replay(row):
        if row.request_hash != digest:
            raise ApiError(409, "IDEMPOTENCY_MISMATCH", "同一冪等鍵不能用於不同的提交內容。")
        if row.status == "SUCCEEDED":
            return row, ok(request, row.response_data, row.response_status, row.response_headers)
        raise ApiError(409, "OPERATION_RUNNING", "此操作正在處理，請用原識別碼查詢或稍後再試。")

    existing = db.scalar(query)
    if existing:
        return replay(existing)
    row = IdempotencyRecord(account_id=principal.account.id, method=request.method,
                            route_scope=scope, key=key, request_hash=digest,
                            expires_at=utcnow() + timedelta(days=7))
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        existing = db.scalar(query)
        if existing:
            return replay(existing)
        raise ApiError(409, "OPERATION_RUNNING", "操作已由另一個請求開始，請稍後查詢。")
    return row, None


def idem_finish(db, record, data, status_code=200, headers=None):
    record.response_data = encode(data)
    record.response_status = status_code
    record.response_headers = headers or {}
    record.status = "SUCCEEDED"
    db.flush()
