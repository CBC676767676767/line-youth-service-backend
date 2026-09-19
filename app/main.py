from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm.exc import StaleDataError
from starlette.exceptions import HTTPException

from app.common import ApiError, meta, new_id, ok
from app.config import Settings
from app.db import Base, make_engine, make_session_factory
from app.web import register_web


def request_limit(path: str, upload_limit: int) -> int:
    if path.startswith("/api/v1/line/simulator/"):
        return 8192
    if path == "/api/v1/precheck/evaluate" or (path.startswith("/api/v1/cases/") and path.endswith("/precheck")):
        return 16_384
    return upload_limit if path.endswith("/content") else 1_048_576


class RequestSizeLimit:
    """Cap streamed bodies too; Content-Length is not an authorization boundary."""
    def __init__(self, app, upload_limit):
        self.app = app
        self.upload_limit = upload_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        maximum = request_limit(scope["path"], self.upload_limit)
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
    from app import admin, auth, cases, files, notifications, precheck, line_handoff, line_simulator
    from app.line_runtime import LineRuntime

    settings = settings or Settings()
    owned_engine = None
    if session_factory is None:
        owned_engine = make_engine(settings.database_url)
        session_factory = make_session_factory(owned_engine)
        if settings.app_env == "test" or settings.auto_create_schema:
            Base.metadata.create_all(owned_engine)

    line_runtime = LineRuntime(settings, session_factory)

    @asynccontextmanager
    async def lifespan(_):
        stop = asyncio.Event()

        async def poll_line_replies():
            while not stop.is_set():
                try:
                    await asyncio.to_thread(line_runtime.tick)
                except Exception as exc:
                    # No exception body, request content, token or SQL parameters.
                    logging.getLogger("youth.line").error("line_reply_tick_failed type=%s", type(exc).__name__)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_seconds)
                except TimeoutError:
                    pass

        # This worker only handles verified inbound reply tokens. It never starts
        # the general notification, export, file scan, SMTP or push workers.
        task = asyncio.create_task(poll_line_replies()) if line_runtime.enabled else None
        application.state.line_reply_task = task
        try:
            yield
        finally:
            stop.set()
            try:
                if task is not None:
                    # Finish the current single-attempt reply before closing its
                    # memory/DB owner; cancellation cannot stop a running thread.
                    await task
            finally:
                try:
                    await asyncio.to_thread(line_runtime.close)
                finally:
                    try:
                        simulator = getattr(application.state, "line_simulator", None)
                        if simulator is not None:
                            simulator.clear()
                    finally:
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
    application.state.line_runtime = line_runtime
    application.state.line_reply_buffer = line_runtime.buffer
    if settings.app_env in {"development", "test"} and settings.line_simulator_enabled:
        application.state.line_simulator = line_simulator.LineSimulator(settings, session_factory)
    application.add_middleware(RequestSizeLimit, upload_limit=settings.max_file_bytes)
    application.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins,
                               allow_credentials=True,
                               allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
                               allow_headers=["Content-Type", "X-CSRF-Token", "If-Match",
                                              "Idempotency-Key", "Authorization", "Range", "X-Upload-Token",
                                              "X-Line-Handoff"],
                               expose_headers=["ETag", "X-Request-ID", "Retry-After", "Content-Range"])

    @application.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = new_id()
        if (request.url.path.startswith("/static/line-simulator.")
                and not (settings.app_env in {"development", "test"} and settings.line_simulator_enabled)):
            return failure(request, 404, "RESOURCE_NOT_FOUND", "找不到此功能。")
        declared = request.headers.get("content-length")
        maximum = request_limit(request.url.path, settings.max_file_bytes)
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
        if request.url.path in {"/precheck", "/line-simulator"} or request.url.path.startswith("/static/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
            response.headers["Cache-Control"] = "no-cache"
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

    @application.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        """Keep driver errors off the re-raise path that would log their parameters.

        Starlette's ServerErrorMiddleware re-raises after the Exception handler
        below, so the server would record a traceback whose SQLAlchemy message
        ends in "[parameters: ...]" — applicant email and whole form payloads.
        Handlers for non-Exception classes run in ExceptionMiddleware, which does
        not re-raise. IntegrityError and StaleDataError still match their own
        handlers first; Starlette resolves by walking the exception's MRO.
        """
        logging.getLogger("youth.api").error("database_error request_id=%s type=%s",
                                             request.state.request_id, type(exc).__name__)
        return failure(request, 500, "INTERNAL_ERROR", "服務暫時無法處理，請提供追蹤編號洽詢。")

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

    for router in [auth.router, cases.router, files.router, notifications.router, admin.router, precheck.router]:
        application.include_router(router, prefix="/api/v1")
    application.include_router(line_handoff.router)
    if settings.app_env in {"development", "test"} and settings.line_simulator_enabled:
        application.include_router(line_simulator.router)
    web_root = Path(__file__).parent / "web"
    application.mount("/static", StaticFiles(directory=web_root, check_dir=False), name="static")

    @application.get("/precheck", include_in_schema=False)
    def precheck_page():
        return FileResponse(web_root / "index.html")

    register_web(application, settings)
    return application


def failure(request, status, code, message, field_errors=None, headers=None):
    if request.url.path == "/api/v1/precheck/evaluate" and status >= 400:
        from app.line_handoff import record_server_failure
        record_server_failure(request)
    return JSONResponse({"error": {"code": code, "message": message,
                                   "field_errors": field_errors or [], "retryable": status >= 500},
                         "meta": meta(request)}, status_code=status, headers=headers)
