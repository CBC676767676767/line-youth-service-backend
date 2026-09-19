"""Single-attempt LINE replies with durable receipts and no stored chat data.

The API and its reply worker must share one LineReplyBuffer. Tokens, menu state
and rendered messages never enter a database, audit record or application log.
A restart can lose a reply; a durable claim prevents dispatching it twice.
API_ACCEPTED means LINE accepted the reply request, not that a person read it.
The live adapter only replies to a verified event's short-lived reply token.
It cannot push to arbitrary recipients, retry, or mutate account settings.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from app.db import new_id, utcnow
from app.line_reply_models import LineReplyJob


REPLY_TTL = timedelta(seconds=50)
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,256}\Z", re.ASCII)


@dataclass(frozen=True, repr=False)
class _ReplyEnvelope:
    command: str
    actor_key: str
    event_key: str
    expires_at: datetime
    delivery_mode: str = "fake"
    reply_token: str | None = None


class LineReplyBuffer:
    """Bounded process-local memory; expired entries are removed each worker tick.

    No representations or counters expose tokens, incoming text or menu choices.
    The owner must clear the buffer on application shutdown.
    """

    def __init__(self, max_items: int = 1000, *, conversations=None):
        if not 1 <= max_items <= 10_000:
            raise ValueError("Reply buffer capacity must be between 1 and 10000")
        self.max_items = max_items
        self._items: dict[str, _ReplyEnvelope] = {}
        self._lock = Lock()
        self._dispatch_lock = Lock()
        if conversations is None:
            from app.line_conversation import ConversationStore
            conversations = ConversationStore()
        self.conversations = conversations

    def _prune(self, now: datetime):
        expired = [key for key, value in self._items.items() if value.expires_at <= now]
        for key in expired:
            del self._items[key]

    def put(self, key: str, value: _ReplyEnvelope) -> bool:
        with self._lock:
            self._prune(utcnow())
            if len(self._items) >= self.max_items or value.expires_at <= utcnow():
                return False
            self._items[key] = value
            return True

    def pending_ids(self, now: datetime | None = None, *, actor_key: str | None = None) -> list[str]:
        with self._lock:
            self._prune(now or utcnow())
            return [key for key, value in self._items.items()
                    if actor_key is None or value.actor_key == actor_key]

    def take(self, key: str) -> _ReplyEnvelope | None:
        with self._lock:
            return self._items.pop(key, None)

    def discard(self, key: str):
        with self._lock:
            self._items.pop(key, None)

    def clear(self):
        with self._dispatch_lock:
            with self._lock:
                self._items.clear()
            self.conversations.clear()


def line_reply_actor_key(settings, subject: str) -> str:
    scope = "youth-service:line-reply-actor:v1\0" + settings.line_messaging_channel_id + "\0" + subject
    return hmac.new(settings.secret_key.encode(), scope.encode(), hashlib.sha256).hexdigest()


def enqueue_line_reply(db, event: dict, settings, *, buffer: LineReplyBuffer,
                       received_at: datetime, event_at: datetime) -> LineReplyJob | None:
    """Join the verified webhook's transaction; do not commit or perform network IO.

    Only normalized commands reach the transient buffer. Callers discard newly
    buffered IDs when their transaction fails. Uncommitted jobs cannot be claimed.
    """
    mode = getattr(settings, "line_reply_mode", "disabled")
    if not getattr(settings, "line_bot_enabled", False) or mode not in {"fake", "live"}:
        return None
    source = event.get("source", {})
    subject = source.get("userId")
    token = event.get("replyToken")
    if (source.get("type") != "user" or not isinstance(subject, str) or not subject
            or event.get("type") not in {"follow", "message", "postback"}
            or event.get("mode", "active") != "active"
            or not isinstance(token, str) or not _TOKEN.fullmatch(token)
            or set(token) == {"0"}):
        return None
    expires_at = min(received_at + REPLY_TTL, event_at + REPLY_TTL)
    job = LineReplyJob(
        id=new_id(), channel_id=settings.line_messaging_channel_id,
        event_id=event["webhookEventId"], event_type=event["type"],
        received_at=received_at, expires_at=expires_at, delivery_mode=mode,
        status="PENDING", attempt_count=0,
    )
    if expires_at <= utcnow():
        job.status, job.error_code, job.completed_at = "EXPIRED", "LINE_REPLY_EXPIRED", utcnow()
    else:
        from app.line_conversation import normalize_event_command
        command = normalize_event_command(event) or "隱私提醒"
        # Live tokens exist only in bounded memory, never on the durable receipt.
        envelope = _ReplyEnvelope(command, line_reply_actor_key(settings, subject),
                                  event["webhookEventId"], expires_at, mode,
                                  token if mode == "live" else None)
        if not buffer.put(job.id, envelope):
            job.status, job.error_code, job.completed_at = "FAILED", "LINE_REPLY_BUFFER_FULL", utcnow()
    db.add(job)
    # The caller's inbox savepoint also includes this unique reply receipt.
    try:
        db.flush()
    except SQLAlchemyError:
        buffer.discard(job.id)
        raise
    return job


@dataclass(frozen=True)
class _ReplyResult:
    status: str
    error_code: str | None = None
    http_status: int | None = None


class FakeLineReplySender:
    """In-memory local recorder. No credential or reply token is accepted.

    Create one per simulator request and immediately take its replies. Capacity
    is bounded even if a caller forgets to consume them. Records are never logged.
    """

    def __init__(self, max_records: int = 100):
        if not 1 <= max_records <= 1000:
            raise ValueError("Fake reply recorder capacity must be between 1 and 1000")
        self.max_records = max_records
        self._records: list[dict] = []
        self._lock = Lock()

    def send(self, *, actor_key: str, messages: list[dict]):
        with self._lock:
            if len(self._records) >= self.max_records:
                raise RuntimeError("Fake reply recorder capacity reached")
            self._records.append({"actor_key": actor_key, "messages": copy.deepcopy(messages)})

    def take_replies(self, actor_key: str | None = None) -> list[dict]:
        with self._lock:
            selected = [row for row in self._records if actor_key is None or row["actor_key"] == actor_key]
            self._records = [row for row in self._records if actor_key is not None and row["actor_key"] != actor_key]
            return selected


class LiveLineReplySender:
    """Fixed-endpoint reply adapter; no recipient, retry, redirect or push API.

    Callers must provide the configured channel token explicitly. Tests inject
    MockTransport; no caller can configure the destination URL or proxy here.
    Only HTTP status survives a response, never its body or request headers.
    """

    def __init__(self, access_token: str, *, transport: httpx.BaseTransport | None = None):
        if (not isinstance(access_token, str) or not 1 <= len(access_token) <= 4096
                or any(char.isspace() or ord(char) < 32 or ord(char) > 126 for char in access_token)):
            raise ValueError("A valid LINE channel access token is required")
        self._access_token = access_token
        self._transport = transport

    def send(self, *, reply_token: str, messages: list[dict]) -> _ReplyResult:
        if (not isinstance(reply_token, str) or not _TOKEN.fullmatch(reply_token)
                or set(reply_token) == {"0"}):
            return _ReplyResult("FAILED", "LINE_REPLY_TOKEN_INVALID")
        if (not isinstance(messages, list) or not 1 <= len(messages) <= 5
                or any(not isinstance(message, dict) for message in messages)):
            return _ReplyResult("FAILED", "LINE_REPLY_MESSAGES_INVALID")
        try:
            with httpx.Client(timeout=8.0, follow_redirects=False, trust_env=False,
                              transport=self._transport) as client:
                # Streaming avoids reading or retaining LINE's response body.
                with client.stream("POST", LINE_REPLY_URL,
                                   headers={"Authorization": "Bearer " + self._access_token},
                                   json={"replyToken": reply_token, "messages": messages}) as response:
                    status = response.status_code
        except httpx.RequestError:
            # Delivery may have reached LINE even when the response was lost.
            return _ReplyResult("UNKNOWN", "LINE_REPLY_NETWORK_UNKNOWN")
        if status == 200:
            return _ReplyResult("API_ACCEPTED", http_status=status)
        if status >= 500:
            return _ReplyResult("UNKNOWN", "LINE_REPLY_SERVER_UNKNOWN", status)
        return _ReplyResult("FAILED", "LINE_REPLY_REJECTED", status)


def _sender_mode(sender):
    if isinstance(sender, LiveLineReplySender):
        return "live"
    if isinstance(sender, FakeLineReplySender):
        return "fake"
    return None


def _deliver(envelope, sender, messages):
    """Exactly one adapter invocation; never recover a failed send with another."""
    try:
        if isinstance(sender, LiveLineReplySender):
            return sender.send(reply_token=envelope.reply_token, messages=messages)
        sender.send(actor_key=envelope.actor_key, messages=messages)
        return _ReplyResult("FAKE_SENT")
    except Exception:
        code = "LINE_REPLY_SEND_UNKNOWN" if isinstance(sender, LiveLineReplySender) else "LINE_FAKE_SENDER_UNKNOWN"
        return _ReplyResult("UNKNOWN", code)


def reply_failure_messages() -> list[dict]:
    """Constant recovery guidance, usable even when the renderer cannot import."""
    try:
        from app.line_conversation import failure_reply
        return failure_reply()
    except Exception:
        return [{"type": "text", "text": "本次聊天操作暫時無法完成，未代為送件。"
                 "請稍後重試，或輸入「選單」返回服務。"}]


def _render_reply(envelope: _ReplyEnvelope, settings, *, store,
                  conversation_now: datetime | None = None) -> list[dict]:
    from app.line_conversation import build_reply
    from app.precheck_engine import load_bundle

    bundle = load_bundle(settings.precheck_rules_path)
    bundle = bundle.model_copy(update={
        "official_application_url": settings.precheck_official_application_url or bundle.official_application_url,
    })
    messages = build_reply(
        envelope.command, envelope.actor_key, bundle, settings.line_channel_secret,
        conversation_now or utcnow(), public_url=getattr(settings, "line_public_precheck_url", "") or None,
        store=store, event_key=envelope.event_key,
    )
    if (not isinstance(messages, list) or not 1 <= len(messages) <= 5
            or any(not isinstance(message, dict) for message in messages)):
        raise ValueError("Reply renderer returned invalid message objects")
    return messages


def _process_line_replies(factory, settings, *, buffer: LineReplyBuffer,
                          sender: FakeLineReplySender | LiveLineReplySender | None = None, limit: int = 20,
                          actor_key: str | None = None,
                          conversation_now: datetime | None = None,
                          conversation_store=None) -> dict[str, int]:
    """Drain only this process's reply buffer, without running private case pushes.

    Tokens expire conservatively 50 seconds after receipt or event time. Orphaned
    PENDING receipts expire after a restart. Claimed SENDING receipts become UNKNOWN
    at expiry and are never reclaimed. Live delivery requires an explicit live
    sender and matching receipt, envelope and current configuration modes.
    Optional test clocks affect conversation state only, never queue TTL.
    """
    if not 1 <= limit <= 100:
        raise ValueError("Reply processing limit must be between 1 and 100")
    sender = sender or FakeLineReplySender()
    active_store = conversation_store if conversation_store is not None else buffer.conversations
    counts = {key: 0 for key in ("processed", "fake_sent", "api_accepted", "expired", "unknown", "failed")}
    now = utcnow()
    with factory() as db:
        for previous, status, code in (
            ("PENDING", "EXPIRED", "LINE_REPLY_EXPIRED"),
            ("SENDING", "UNKNOWN", "LINE_REPLY_PROCESS_LOST"),
        ):
            expired = db.execute(update(LineReplyJob).where(
                LineReplyJob.status == previous, LineReplyJob.expires_at <= now,
                LineReplyJob.channel_id == settings.line_messaging_channel_id,
            ).values(status=status, completed_at=now, error_code=code,
                     version=LineReplyJob.version + 1))
            counts[status.lower()] += expired.rowcount
            counts["processed"] += expired.rowcount
        db.commit()
    ids = buffer.pending_ids(now, actor_key=actor_key)
    if not ids:
        return counts
    with factory() as db:
        eligible = dict(db.execute(select(LineReplyJob.id, LineReplyJob.delivery_mode).where(
            LineReplyJob.id.in_(ids), LineReplyJob.status == "PENDING",
            LineReplyJob.channel_id == settings.line_messaging_channel_id,
            LineReplyJob.expires_at > now,
        )).all())
    # A webhook batch shares received_at; UUID ordering would scramble commands.
    candidates = [job_id for job_id in ids if job_id in eligible][:limit]
    for job_id in candidates:
        with factory() as db:
            claimed = db.execute(update(LineReplyJob).where(
                LineReplyJob.id == job_id, LineReplyJob.status == "PENDING",
                LineReplyJob.channel_id == settings.line_messaging_channel_id,
                LineReplyJob.delivery_mode == eligible[job_id],
                LineReplyJob.expires_at > utcnow(),
            ).values(status="SENDING", started_at=utcnow(), attempt_count=1,
                     version=LineReplyJob.version + 1))
            db.commit()
            if not claimed.rowcount:
                continue
        # Remove sensitive memory immediately after a durable claim, before IO.
        envelope = buffer.take(job_id)
        active_mode = getattr(settings, "line_reply_mode", "disabled")
        if envelope is None:
            outcome = _ReplyResult("FAILED", "LINE_REPLY_MEMORY_UNAVAILABLE")
        elif not getattr(settings, "line_bot_enabled", False) or active_mode not in {"fake", "live"}:
            outcome = _ReplyResult("FAILED", "LINE_REPLIES_DISABLED")
        elif not active_mode == eligible[job_id] == envelope.delivery_mode == _sender_mode(sender):
            outcome = _ReplyResult("FAILED", "LINE_REPLY_MODE_MISMATCH")
        elif envelope.expires_at <= utcnow():
            outcome = _ReplyResult("EXPIRED", "LINE_REPLY_EXPIRED")
        else:
            try:
                messages = _render_reply(envelope, settings, store=active_store,
                                         conversation_now=conversation_now)
            except Exception:
                # Do not expose exception text containing incoming data or paths.
                try:
                    from app.line_conversation import is_precheck_command, record_actor_failure
                    if is_precheck_command(envelope.command):
                        record_actor_failure(envelope.actor_key, store=active_store,
                                             now=conversation_now or utcnow())
                except Exception:
                    # Recovery still works if the renderer module itself failed.
                    # Safety-practice failures never invalidate precheck results.
                    pass
                if envelope.expires_at <= utcnow():
                    outcome = _ReplyResult("EXPIRED", "LINE_REPLY_EXPIRED")
                else:
                    outcome = _deliver(envelope, sender, reply_failure_messages())
                    if outcome.status in {"FAKE_SENT", "API_ACCEPTED"}:
                        # Accepted failure guidance is not a successful precheck.
                        outcome = _ReplyResult("FAILED", "LINE_REPLY_BUILD_FAILED", outcome.http_status)
            else:
                if envelope.expires_at <= utcnow():
                    outcome = _ReplyResult("EXPIRED", "LINE_REPLY_EXPIRED")
                else:
                    outcome = _deliver(envelope, sender, messages)
                del messages
        del envelope
        with factory() as db:
            completed = db.execute(update(LineReplyJob).where(
                LineReplyJob.id == job_id, LineReplyJob.status == "SENDING",
            ).values(status=outcome.status, completed_at=utcnow(),
                     error_code=outcome.error_code, http_status=outcome.http_status,
                     version=LineReplyJob.version + 1))
            db.commit()
            if completed.rowcount:
                counts["processed"] += 1
                counts[outcome.status.lower()] += 1
    return counts


def process_line_replies(factory, settings, *, buffer: LineReplyBuffer,
                         sender: FakeLineReplySender | LiveLineReplySender | None = None, limit: int = 20,
                         actor_key: str | None = None,
                         conversation_now: datetime | None = None,
                         conversation_store=None) -> dict[str, int]:
    """Serialize dispatches so same-actor commands cannot overtake.

    Claims remain durable so another process cannot replay a job after memory
    is lost. The global notification/push worker is never called here.
    """
    with buffer._dispatch_lock:
        return _process_line_replies(factory, settings, buffer=buffer, sender=sender,
                                     limit=limit, actor_key=actor_key, conversation_now=conversation_now,
                                     conversation_store=conversation_store)
