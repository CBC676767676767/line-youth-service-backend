from __future__ import annotations

from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from starlette.exceptions import HTTPException

from app.common import ApiError, meta, new_id, ok
from app.config import Settings
from app.db import Base, make_engine, make_session_factory
from app.web import register_web


class RequestSizeLimit:
    """Cap streamed bodies too; Content-Length is not an authorization boundary."""
    def __init__(self, app, upload_limit):
        self.app = app
        self.upload_limit = upload_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        maximum = self.upload_limit if scope["path"].endswith("/content") else 1_048_576
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > maximum:
                    raise HTTPException(413, "請求超過允許容量。")
            return message

        return await self.app(scope, bounded_receive, send)


def create_app(settings: Settings | None = None, session_factory=None):
    from app import admin, auth, cases, files, notifications

    settings = settings or Settings()
    owned_engine = None
    if session_factory is None:
        owned_engine = make_engine(settings.database_url)
        session_factory = make_session_factory(owned_engine)
        if settings.app_env == "test" or settings.auto_create_schema:
            Base.metadata.create_all(owned_engine)

    @asynccontextmanager
    async def lifespan(_):
        yield
        if owned_engine is not None:
            owned_engine.dispose()

    application = FastAPI(
        title="青年申請與案件服務 API", version="0.1.0", lifespan=lifespan,
        description="官方 LINE 入口的案件、補件、審查及通知服務。\n\n"
                    "登入取得 Cookie 與 csrf_token 後，修改請求須帶 X-CSRF-Token；"
                    "資源修改帶 If-Match，正式送件帶 Idempotency-Key。",
        docs_url="/docs" if settings.app_env != "production" else None,
        redoc_url="/redoc" if settings.app_env != "production" else None,
    )
    application.state.settings = settings
    application.state.session_factory = session_factory
    application.add_middleware(RequestSizeLimit, upload_limit=settings.max_file_bytes)
    application.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins,
                               allow_credentials=True,
                               allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
                               allow_headers=["Content-Type", "X-CSRF-Token", "If-Match",
                                              "Idempotency-Key", "Authorization", "Range", "X-Upload-Token"],
                               expose_headers=["ETag", "X-Request-ID", "Retry-After", "Content-Range"])

    @application.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = new_id()
        declared = request.headers.get("content-length")
        maximum = settings.max_file_bytes if request.url.path.endswith("/content") else 1_048_576
        if declared:
            try:
                if int(declared) < 0 or int(declared) > maximum:
                    return failure(request, 413, "REQUEST_TOO_LARGE", "請求超過允許容量。")
            except ValueError:
                return failure(request, 400, "INVALID_CONTENT_LENGTH", "無效的請求長度。")
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "private, no-store"
        return response

    @application.exception_handler(ApiError)
    async def api_error(request, exc):
        return failure(request, exc.status, exc.code, exc.message, exc.field_errors,
                       headers=getattr(exc, "headers", None))

    @application.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        errors = [{"field": ".".join(str(v) for v in err["loc"]),
                   "message": err["msg"], "code": err["type"]} for err in exc.errors()]
        return failure(request, 422, "VALIDATION_ERROR", "請檢查輸入欄位。", errors)

    @application.exception_handler(StaleDataError)
    async def stale_error(request, exc):
        return failure(request, 412, "VERSION_CONFLICT", "資料已被其他操作更新。")

    @application.exception_handler(IntegrityError)
    async def integrity_error(request, exc):
        return failure(request, 409, "DATA_CONFLICT", "資料與現有紀錄衝突，請重新確認。")

    @application.exception_handler(HTTPException)
    async def http_error(request, exc):
        return failure(request, exc.status_code, "HTTP_ERROR", str(exc.detail), headers=exc.headers)

    @application.exception_handler(Exception)
    async def unexpected_error(request, exc):
        # Do not log request bodies, credentials, SQL bound parameters or PII.
        logging.getLogger("youth.api").error("unhandled_request_error request_id=%s type=%s",
                                             request.state.request_id, type(exc).__name__)
        return failure(request, 500, "INTERNAL_ERROR", "服務暫時無法處理，請提供追蹤編號洽詢。")

    @application.get("/health/live", tags=["health"])
    def live(request: Request):
        return ok(request, {"status": "ok"})

    @application.get("/health/ready", tags=["health"])
    def ready(request: Request):
        try:
            with session_factory() as db:
                db.execute(text("SELECT 1 FROM schemes LIMIT 1"))
        except Exception:
            return failure(request, 503, "NOT_READY", "資料庫或資料結構尚未就緒。")
        return ok(request, {"status": "ready", "environment": settings.app_env})

    for router in [auth.router, cases.router, files.router, notifications.router, admin.router]:
        application.include_router(router, prefix="/api/v1")
    register_web(application, settings)
    return application


def failure(request, status, code, message, field_errors=None, headers=None):
    return JSONResponse({"error": {"code": code, "message": message,
                                   "field_errors": field_errors or [], "retryable": status >= 500},
                         "meta": meta(request)}, status_code=status, headers=headers)
