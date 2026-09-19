"""Transient LINE-to-web routing hints; never an identity or case-access credential."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import RLock
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.access import case_for
from app.auth import Principal, current_principal
from app.common import ApiError, ok
from app.db import get_db, utcnow
from app.line_conversation import (
    HANDOFF_RE, STATE_TTL_SECONDS, _current_handoff, _time, _version,
    read_handoff, record_handoff_failure, record_handoff_result,
)
from app.precheck_engine import load_bundle
from app.precheck_models import PrecheckSnapshot
from app.precheck_schema import PrecheckInput


router = APIRouter(tags=["line-handoff"])
PRIVATE_HEADERS = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"}
CHOICE_FIELDS = frozenset({"purchase_stage", "tool_id", "billing_type", "billing_component", "purchase_channel"})


@dataclass(frozen=True, repr=False)
class CaseHandoff:
    owner_id: str
    case_id: str
    snapshot_id: str
    expires_at: datetime


class CaseHandoffRegistry:
    """Bounded, locked memory only. Existing ownership survives until its original TTL."""

    def __init__(self, max_items=1000):
        if type(max_items) is not int or not 1 <= max_items <= 10_000:
            raise ValueError("Case handoff capacity must be between 1 and 10000")
        self.max_items = max_items
        self._items = OrderedDict()
        self._lock = RLock()

    def _purge(self, now):
        for key in [key for key, value in self._items.items() if value.expires_at <= now]:
            del self._items[key]

    def bind(self, token, *, owner_id, case_id, snapshot_id, expires_at, now=None, on_bind=None):
        now = now or utcnow()
        if not isinstance(token, str) or not HANDOFF_RE.fullmatch(token) or expires_at <= now:
            return False
        if any(not isinstance(value, str) or not 1 <= len(value) <= 128
               for value in (owner_id, case_id, snapshot_id)):
            return False
        with self._lock:
            self._purge(now)
            previous = self._items.get(token)
            if previous and previous.owner_id != owner_id:
                return False
            if previous is None and len(self._items) >= self.max_items:
                # Do not evict a live binding and permit a second account to claim it.
                return False
            expiry = min(expires_at, now + timedelta(seconds=STATE_TTL_SECONDS))
            if previous:
                expiry = min(expiry, previous.expires_at)
            if on_bind is not None and not on_bind():
                return False
            self._items[token] = CaseHandoff(owner_id, case_id, snapshot_id, expiry)
            return True

    def get(self, token, *, now=None):
        if not isinstance(token, str) or not HANDOFF_RE.fullmatch(token):
            return None
        with self._lock:
            self._purge(now or utcnow())
            return self._items.get(token)

    def clear(self):
        with self._lock:
            self._items.clear()


@dataclass(frozen=True, repr=False)
class _HandoffContext:
    runtime: object
    store: object
    now: datetime
    expires_at: datetime
    choices: dict


def _registry(runtime):
    registry = getattr(runtime, "case_handoffs", None)
    if not isinstance(registry, CaseHandoffRegistry):
        registry = CaseHandoffRegistry()
        runtime.case_handoffs = registry
    return registry


def _context(request, token):
    if not isinstance(token, str) or not HANDOFF_RE.fullmatch(token):
        return None
    settings = request.app.state.settings
    live = getattr(request.app.state, "line_runtime", None)
    candidates = []
    if (live is not None and getattr(live, "enabled", False)
            and settings.line_bot_enabled and settings.line_reply_mode == "live"):
        candidates.append((live, live.buffer.conversations, utcnow(), settings))
    # Legacy simulator stores remain available only inside explicitly enabled test runs.
    simulator = getattr(request.app.state, "line_simulator", None)
    if settings.app_env == "test" and settings.line_simulator_enabled and simulator is not None:
        store = simulator.handoff_store(token)
        if store is not None:
            candidates.append((simulator, store, simulator.handoff_time(token), simulator.settings))
    for runtime, store, now, runtime_settings in candidates:
        try:
            bundle = load_bundle(runtime_settings.precheck_rules_path)
            bundle = bundle.model_copy(update={
                "official_application_url": runtime_settings.precheck_official_application_url or bundle.official_application_url,
            })
            choices = read_handoff(token, store=store, now=now)
            if not choices:
                continue
            with store.lock:
                current = _current_handoff(token, store, _time(now))
                if not current or current[1]["version"] != _version(bundle):
                    continue
                expires_at = datetime.fromtimestamp(current[0]["expires"], now.tzinfo)
            # This endpoint never returns free text, identifiers, document data or full form inputs.
            selected = {key: value for key, value in choices.items() if key in CHOICE_FIELDS}
            validated = PrecheckInput.model_validate(selected).model_dump(mode="json")
            return _HandoffContext(runtime, store, now, expires_at,
                                   {key: validated[key] for key in CHOICE_FIELDS})
        except (OSError, ValueError, TypeError, KeyError):
            # Invalid or newly unavailable policy leaves no stale routing authority.
            continue
    return None


@router.get("/api/v1/line/handoffs/{token}")
def get_handoff(token: str, request: Request):
    context = _context(request, token)
    if context is None:
        raise ApiError(410, "HANDOFF_EXPIRED", "聊天交接已失效，請重新開始預檢。")
    return ok(request, {"choices": context.choices,
                        "notice": "只帶入非敏感選擇，不代表已登入、已保存或身分已查證。"},
              headers=PRIVATE_HEADERS)


@router.get("/api/v1/line/results/{token}")
def get_saved_result(token: str, request: Request,
                     principal: Annotated[Principal, Depends(current_principal)],
                     db: Annotated[Session, Depends(get_db)]):
    context = _context(request, token)
    binding = _registry(context.runtime).get(token, now=context.now) if context else None
    if binding is None or binding.owner_id != principal.account.id:
        raise ApiError(404, "LINE_RESULT_UNAVAILABLE", "此結果連結目前無法使用，請登入本人帳號或重新開始預檢。")
    try:
        case = case_for(db, principal, binding.case_id)
    except ApiError:
        raise ApiError(404, "LINE_RESULT_UNAVAILABLE", "此結果連結目前無法使用，請登入本人帳號或重新開始預檢。") from None
    if case.created_by != principal.account.id:
        raise ApiError(404, "LINE_RESULT_UNAVAILABLE", "此結果連結目前無法使用，請登入本人帳號或重新開始預檢。")
    snapshot = db.get(PrecheckSnapshot, binding.snapshot_id)
    if snapshot is None or snapshot.case_id != case.id:
        raise ApiError(404, "LINE_RESULT_UNAVAILABLE", "此結果連結目前無法使用，請登入本人帳號或重新開始預檢。")
    return ok(request, {"case_id": case.id, "snapshot_id": snapshot.id}, headers=PRIVATE_HEADERS)


def record_server_result(request: Request, result: dict) -> bool:
    """Anonymous evaluation can update only the conversation's safe, explicitly unsaved counts."""
    token = request.headers.get("X-Line-Handoff", "")
    context = _context(request, token)
    return bool(context and record_handoff_result(token, result, store=context.store, now=context.now, saved=False))


def record_server_failure(request: Request) -> bool:
    token = request.headers.get("X-Line-Handoff", "")
    context = _context(request, token)
    return bool(context and record_handoff_failure(token, store=context.store, now=context.now))


def record_saved_result(request: Request, principal: Principal, case, snapshot: dict) -> bool:
    """Call only after commit/access-checked replay. A routing hint never expands account access."""
    token = request.headers.get("X-Line-Handoff", "")
    if case.created_by != principal.account.id:
        return False
    context = _context(request, token)
    if context is None:
        return False
    return _registry(context.runtime).bind(
        token, owner_id=principal.account.id, case_id=case.id, snapshot_id=snapshot["id"],
        expires_at=context.expires_at, now=context.now,
        on_bind=lambda: record_handoff_result(token, snapshot["result"], store=context.store,
                                              now=context.now, saved=True),
    )
