"""Loopback-only LINE conversation simulator using the real signed webhook.

Synthetic channel credentials and users are isolated from the configured LINE bot.
Only delivery receipts are durable; conversations and handoffs remain in memory.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.auth import _origin
from app.common import ApiError, ok
from app.db import utcnow
from app.line_replies import FakeLineReplySender, LineReplyBuffer, line_reply_actor_key, process_line_replies


router = APIRouter(tags=["local-line-simulator"])
SESSION_TTL = timedelta(hours=1)
DATA_ROOT = Path(__file__).parent / "data"
WEB_ROOT = Path(__file__).parent / "web"


class SimulatorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: Annotated[str, Field(min_length=20, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")]


class SimulatorEvent(SimulatorInput):
    kind: Literal["text", "postback", "attachment", "failure"] = "text"
    text: str = Field(default="", max_length=1000)
    data: str = Field(default="", max_length=300)
    repeat_last: bool = False


class SimulatorAdvance(SimulatorInput):
    minutes: int = Field(default=25, ge=1, le=60)


@dataclass(repr=False)
class SimSession:
    subject: str
    actor: str
    touched: datetime
    conversations: object
    offset: timedelta = timedelta()
    last_event: dict | None = None
    event_count: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class LineSimulator:
    def __init__(self, settings, factory):
        from app.notifications import router as webhook_router

        self.sessions: dict[str, SimSession] = {}
        self.handoff_sessions: dict[str, str] = {}
        self.factory = factory
        self.settings = settings.model_copy(update={
            "line_channel_secret": secrets.token_hex(32),
            "line_messaging_channel_id": "local-simulator-" + secrets.token_hex(8),
            "line_destination_user_id": "U" + "0" * 32,
            "line_channel_access_token": "", "line_provider_id": "", "line_channel_id": "",
            "line_bot_enabled": True, "line_reply_mode": "fake",
            "line_public_precheck_url": settings.public_origin.rstrip("/") + "/precheck",
        })
        self.buffer = LineReplyBuffer()
        self.child = FastAPI()
        self.child.state.settings = self.settings
        self.child.state.session_factory = factory
        self.child.state.line_reply_buffer = self.buffer

        @self.child.exception_handler(ApiError)
        async def error(_request, exc):
            return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)

        self.child.include_router(webhook_router, prefix="/api/v1")

    def create_session(self):
        from app.line_conversation import ConversationStore
        now = utcnow()
        for key in [key for key, row in self.sessions.items() if now - row.touched > SESSION_TTL]:
            self.sessions[key].conversations.clear()
            del self.sessions[key]
        self.handoff_sessions = {key: session for key, session in self.handoff_sessions.items()
                                 if session in self.sessions}
        if len(self.sessions) >= 100:
            raise ApiError(429, "SIMULATOR_CAPACITY", "本機模擬工作階段已達上限，請稍後重試。")
        token = secrets.token_urlsafe(24)
        subject = "U" + secrets.token_hex(16)
        self.sessions[token] = SimSession(subject, line_reply_actor_key(self.settings, subject), now,
                                         ConversationStore(max_sessions=4, max_handoffs=12, max_events=60))
        return token

    def session(self, token):
        row = self.sessions.get(token)
        if not row or utcnow() - row.touched > SESSION_TTL:
            self.sessions.pop(token, None)
            raise ApiError(410, "SIMULATOR_EXPIRED", "模擬對話已過期，請重新開始。")
        row.touched = utcnow()
        return row

    def state(self, row):
        from app.line_conversation import conversation_state
        return conversation_state(row.actor, store=row.conversations, now=utcnow() + row.offset)

    async def dispatch(self, token, incoming):
        row = self.session(token)
        async with row.lock:
            if incoming.repeat_last:
                if row.last_event is None:
                    raise ApiError(409, "NO_EVENT_TO_REPLAY", "請先送出一個合成事件。")
                event = row.last_event
            else:
                if row.event_count >= 500:
                    raise ApiError(429, "SIMULATOR_EVENT_LIMIT", "此模擬對話已達操作上限，請重新開始。")
                event = {"webhookEventId": "sim-" + secrets.token_hex(16),
                         "type": "postback" if incoming.kind == "postback" else "message",
                         "timestamp": int(utcnow().timestamp() * 1000), "mode": "active",
                         "replyToken": "synthetic-reply-token",
                         "source": {"type": "user", "userId": row.subject}}
                if incoming.kind == "postback":
                    event["postback"] = {"data": incoming.data}
                elif incoming.kind == "attachment":
                    event["message"] = {"id": "synthetic-attachment", "type": "image"}
                else:
                    event["message"] = {"id": "synthetic-text", "type": "text", "text": incoming.text}
                row.last_event = event
                row.event_count += 1
            raw = json.dumps({"destination": self.settings.line_destination_user_id,
                              "events": [event]}, ensure_ascii=False, separators=(",", ":")).encode()
            signature = base64.b64encode(hmac.new(self.settings.line_channel_secret.encode(), raw,
                                                 hashlib.sha256).digest()).decode()
            # Same router/signature/deduplication boundary; transport cannot leave this process.
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.child),
                                         base_url="http://local-simulator", trust_env=False) as client:
                response = await client.post("/api/v1/webhooks/line", content=raw,
                                             headers={"Content-Type": "application/json", "X-Line-Signature": signature})
            if response.status_code != 200:
                raise ApiError(503, "SIMULATOR_WEBHOOK_FAILED", "合成事件未被接收，請稍後重試。")
            duplicate = response.json()["data"]["new_events"] == 0
            sender = FakeLineReplySender()
            render_settings = self.settings.model_copy(update={"precheck_rules_path": DATA_ROOT / "missing-simulator-rule.json"}) \
                if incoming.kind == "failure" else self.settings
            counts = process_line_replies(self.factory, render_settings, buffer=self.buffer, sender=sender,
                                          actor_key=row.actor, conversation_now=utcnow() + row.offset,
                                          conversation_store=row.conversations)
            messages = [message for reply in sender.take_replies(row.actor) for message in reply["messages"]]
            def remember_links(value):
                if isinstance(value, dict):
                    if value.get("type") == "uri" and isinstance(value.get("uri"), str):
                        candidates = parse_qs(urlsplit(value["uri"]).query).get("line_handoff", [])
                        for candidate in candidates:
                            if 16 <= len(candidate) <= 256:
                                if len(self.handoff_sessions) >= 1000:
                                    self.handoff_sessions.pop(next(iter(self.handoff_sessions)))
                                self.handoff_sessions[candidate] = token
                    for child in value.values():
                        remember_links(child)
                elif isinstance(value, list):
                    for child in value:
                        remember_links(child)
            remember_links(messages)
            return {"messages": messages, "event_id": event["webhookEventId"], "duplicate": duplicate,
                    "state": self.state(row), "delivery": "fake", "counts": counts,
                    "notice": "本機合成事件與 fake sender；未發送真實 LINE 訊息。"}

    def clear(self):
        for row in self.sessions.values():
            row.conversations.clear()
        self.sessions.clear()
        self.handoff_sessions.clear()
        self.buffer.clear()

    def handoff_time(self, token):
        row = self.sessions.get(self.handoff_sessions.get(token, ""))
        return utcnow() + row.offset if row else utcnow()

    def handoff_store(self, token):
        row = self.sessions.get(self.handoff_sessions.get(token, ""))
        return row.conversations if row else None


def require_local(request: Request) -> LineSimulator:
    settings = request.app.state.settings
    if settings.app_env not in {"test", "development"} or not settings.line_simulator_enabled:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "找不到此功能。")
    host = request.client.host if request.client else ""
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = settings.app_env == "test" and host == "testclient"
    if not local:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "找不到此功能。")
    _origin(request)
    runtime = getattr(request.app.state, "line_simulator", None)
    if runtime is None:
        runtime = LineSimulator(settings, request.app.state.session_factory)
        request.app.state.line_simulator = runtime
    return runtime


@router.get("/line-simulator", include_in_schema=False)
def simulator_page(request: Request):
    require_local(request)
    return FileResponse(WEB_ROOT / "line-simulator.html", headers={"Cache-Control": "no-store"})


@router.get("/line-simulator-assets/menu.png", include_in_schema=False)
def menu_image(request: Request):
    require_local(request)
    return FileResponse(DATA_ROOT / "line-rich-menu.png", headers={"Cache-Control": "no-store"})


@router.post("/api/v1/line/simulator/sessions")
async def create_session(request: Request):
    runtime = require_local(request)
    token = runtime.create_session()
    welcome = await runtime.dispatch(token, SimulatorEvent(session_id=token, kind="text", text="選單"))
    menu = json.loads((DATA_ROOT / "line-rich-menu.json").read_text(encoding="utf-8"))
    return ok(request, {"session_id": token, **welcome, "rich_menu": {
        **menu, "image_url": "/line-simulator-assets/menu.png",
    }}, headers={"Cache-Control": "private, no-store"})


@router.post("/api/v1/line/simulator/events")
async def dispatch_event(body: SimulatorEvent, request: Request):
    runtime = require_local(request)
    return ok(request, await runtime.dispatch(body.session_id, body), headers={"Cache-Control": "private, no-store"})


@router.post("/api/v1/line/simulator/advance")
async def advance_clock(body: SimulatorAdvance, request: Request):
    runtime = require_local(request)
    row = runtime.session(body.session_id)
    async with row.lock:
        row.offset += timedelta(minutes=body.minutes)
        if row.offset > timedelta(days=1):
            row.offset = timedelta(days=1)
    return ok(request, {"advanced": True, "state": runtime.state(row), "delivery": "fake"})


@router.get("/api/v1/line/simulator/templates")
def template_gallery(request: Request):
    from app.line_conversation import result_reply_from_backend
    from app.precheck_engine import TAIPEI, evaluate, load_bundle
    from app.precheck_schema import PrecheckInput

    runtime = require_local(request)
    bundle = load_bundle(DATA_ROOT / "precheck-demo.json")
    fixture = {
        "purchase_stage": "purchased", "tool_id": "demo-studio", "plan_id": "monthly-credit",
        "billing_type": "monthly", "purchase_channel": "official", "purchase_url": "https://studio.example",
        "purchase_date": "2026-09-01", "subscription_start": "2026-09-01", "subscription_end": "2026-10-01",
        "birth_date": "2000-01-01", "residency": "hsinchu", "application_type": "standard", "payer": "self",
    }
    cards = []
    scenarios = [("自填檢查未發現異常", {}), ("依填答有待處理事项", {"purchase_channel": "agent"}),
                 ("未知方案需人工確認", {"plan_id": None, "plan_name": "合成未知方案"})]
    for label, changes in scenarios:
        result = evaluate(PrecheckInput(**{**fixture, **changes}), bundle,
                          now=datetime(2026, 9, 19, 12, tzinfo=TAIPEI), demo_allowed=True)
        cards.append({"label": label, "messages": result_reply_from_backend(
            result, public_url=runtime.settings.line_public_precheck_url)})
    return ok(request, {"mode": "demo", "cards": cards,
                        "notice": "由合成規則及測試資料計算，僅展示模板，不是目前對話或案件的結果。"})


def get_handoff(token: str, request: Request):
    from app.line_conversation import read_handoff

    runtime = require_local(request)
    if len(token) > 256:
        raise ApiError(410, "HANDOFF_EXPIRED", "聊天交接已失效，請重新開始預檢。")
    store = runtime.handoff_store(token)
    choices = read_handoff(token, store=store, now=runtime.handoff_time(token)) if store else None
    if not choices:
        raise ApiError(410, "HANDOFF_EXPIRED", "聊天交接已失效，請重新開始預檢。")
    return ok(request, {"choices": choices,
                        "notice": "只帶入非敏感選擇，不代表已登入、保存或身分已查證。"},
              headers={"Cache-Control": "private, no-store"})


def record_server_result(request: Request, result: dict) -> None:
    """Optional correlation cannot grant identity or affect an evaluation/save."""
    token = request.headers.get("X-Line-Handoff", "")
    runtime = getattr(request.app.state, "line_simulator", None)
    if not token or len(token) > 256 or runtime is None:
        return
    from app.line_conversation import record_handoff_result

    store = runtime.handoff_store(token)
    if store:
        record_handoff_result(token, result, store=store, now=runtime.handoff_time(token))


def record_server_failure(request: Request) -> None:
    """A failed subsequent web evaluation must not leave an old success current."""
    token = request.headers.get("X-Line-Handoff", "")
    runtime = getattr(request.app.state, "line_simulator", None)
    if not token or len(token) > 256 or runtime is None:
        return
    from app.line_conversation import record_handoff_failure
    store = runtime.handoff_store(token)
    if store:
        record_handoff_failure(token, store=store, now=runtime.handoff_time(token))
