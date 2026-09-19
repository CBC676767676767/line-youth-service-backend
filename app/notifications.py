"""Persistent notification outbox API and signed LINE webhook inbox."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.access import case_filter, case_for
from app.auth import Principal, current_principal
from app.common import ApiError, audit, check_version, etag, ok
from app.db import get_db, new_id, utcnow
from app.line_replies import LineReplyBuffer, enqueue_line_reply
from app.models import (
    Case, IdentityLink, NotificationAttempt, NotificationIntent, OutboxJob, WebhookInbox,
)
from app.notification_delivery import PURPOSE_LABELS, verify_signature


router = APIRouter(tags=["notifications"])


def enqueue_notification(db: Session, *, event_id: str, case_id: str, account_id: str,
                         purpose: str, task_id: str | None = None,
                         task_revision: int | None = None, scheduled_at=None, payload=None):
    """Add intents and jobs to the caller's business transaction; never commit.

Payloads are generated from a reviewed generic purpose catalogue. Arbitrary
caller text is deliberately not forwarded into lock-screen LINE previews.
"""
    scheduled_at = scheduled_at or utcnow()
    link = db.scalar(select(IdentityLink).where(
        IdentityLink.account_id == account_id, IdentityLink.revoked_at.is_(None),
    ))
    channels = [("IN_APP", None)] + ([("LINE", link.binding_version)] if link else [])
    results = []
    for channel, binding_version in channels:
        existing = db.scalar(select(NotificationIntent).where(
            NotificationIntent.event_id == event_id, NotificationIntent.purpose == purpose,
            NotificationIntent.channel == channel, NotificationIntent.account_id == account_id,
        ))
        if existing:
            results.append(existing)
            continue
        intent = NotificationIntent(
            id=new_id(), event_id=event_id, case_id=case_id, task_id=task_id,
            task_revision=task_revision, account_id=account_id, binding_version=binding_version,
            channel=channel, purpose=purpose, scheduled_at=scheduled_at,
            status="PLANNED" if scheduled_at > utcnow() else "READY", payload_snapshot={},
        )
        db.add(intent)
        db.flush()
        db.add(OutboxJob(intent_id=intent.id, available_at=scheduled_at, attempt_count=0))
        results.append(intent)
    return results


def public_notification(intent):
    return {
        "id": intent.id, "case_id": intent.case_id, "task_id": intent.task_id,
        "channel": intent.channel, "purpose": intent.purpose,
        "summary": PURPOSE_LABELS.get(intent.purpose, "案件服務有更新，請登入查看"),
        "target_path": f"/tasks/{intent.task_id}" if intent.task_id else f"/cases/{intent.case_id}",
        "status": intent.status, "scheduled_at": intent.scheduled_at,
        "viewed_at": intent.viewed_at, "version": intent.version,
        "acceptance_meaning": "站內通知已發布" if intent.channel == "IN_APP" else "LINE API 已接受；不代表送達或已讀",
    }


@router.get("/notifications")
def list_notifications(request: Request, cursor: str | None = None, limit: int = Query(20, ge=1, le=100),
                       principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    query = select(NotificationIntent).join(Case, NotificationIntent.case_id == Case.id).where(
        NotificationIntent.account_id == principal.account.id,
        NotificationIntent.channel == "IN_APP", NotificationIntent.status == "API_ACCEPTED",
        case_filter(principal),
    )
    if cursor:
        query = query.where(NotificationIntent.id < cursor)
    rows = db.scalars(query.order_by(NotificationIntent.id.desc()).limit(limit + 1)).all()
    return ok(request, {"items": [public_notification(row) for row in rows[:limit]],
                        "next_cursor": rows[limit - 1].id if len(rows) > limit else None})


@router.post("/notifications/{notification_id}/view", status_code=204)
def view_notification(notification_id: str, request: Request,
                      principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    intent = db.get(NotificationIntent, notification_id)
    if not intent or intent.account_id != principal.account.id or intent.channel != "IN_APP":
        raise ApiError(404, "RESOURCE_NOT_FOUND", "找不到可查看的通知。")
    case_for(db, principal, intent.case_id)
    if intent.status != "API_ACCEPTED":
        raise ApiError(409, "NOTIFICATION_NOT_PUBLISHED", "通知尚未發布。")
    if intent.viewed_at is None:
        intent.viewed_at = utcnow()
        audit(db, request, actor_id=principal.account.id, action="NOTIFICATION_VIEWED",
              resource_id=intent.id, case_id=intent.case_id)
        db.commit()
    return Response(status_code=204)


def _staff_intent(db, principal, notification_id):
    if not principal.roles.intersection({"reviewer", "supervisor"}):
        raise ApiError(403, "FORBIDDEN", "目前角色不可管理案件通知。")
    intent = db.get(NotificationIntent, notification_id)
    if not intent:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "找不到可管理的通知。")
    case_for(db, principal, intent.case_id)
    return intent


def _retry_allowed(intent, now):
    if intent.status not in {"RETRY_WAIT", "DEAD_LETTER"}:
        return False
    if intent.first_attempt_at and now - intent.first_attempt_at >= timedelta(hours=24, seconds=-30):
        return False
    return intent.channel == "LINE"


@router.get("/staff/notifications")
def staff_notifications(request: Request, case_id: str | None = None, status: str | None = None,
                        cursor: str | None = None, limit: int = Query(20, ge=1, le=100),
                        principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    if not principal.roles.intersection({"reviewer", "supervisor"}):
        raise ApiError(403, "FORBIDDEN", "目前角色不可管理案件通知。")
    query = select(NotificationIntent).join(Case, NotificationIntent.case_id == Case.id).where(case_filter(principal))
    if case_id:
        case_for(db, principal, case_id)
        query = query.where(NotificationIntent.case_id == case_id)
    if status:
        query = query.where(NotificationIntent.status == status)
    if cursor:
        query = query.where(NotificationIntent.id < cursor)
    rows = db.scalars(query.order_by(NotificationIntent.id.desc()).limit(limit + 1)).all()
    return ok(request, {"items": [public_notification(row) for row in rows[:limit]],
                        "next_cursor": rows[limit - 1].id if len(rows) > limit else None})


@router.get("/staff/notifications/{notification_id}")
def staff_notification(notification_id: str, request: Request,
                        principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    intent = _staff_intent(db, principal, notification_id)
    attempts = db.scalars(select(NotificationAttempt).where(
        NotificationAttempt.intent_id == intent.id
    ).order_by(NotificationAttempt.attempt_no)).all()
    data = public_notification(intent)
    data.update({
        "last_error_code": intent.last_error_code,
        "allowed_actions": ["retry_notification"] if _retry_allowed(intent, utcnow()) else [],
        "attempts": [{"attempt_no": row.attempt_no, "attempted_at": row.attempted_at,
                      "http_status": row.http_status, "result": row.result,
                      "provider_request_id": row.provider_request_id,
                      "accepted_request_id": row.accepted_request_id} for row in attempts],
    })
    return ok(request, data, headers={"ETag": etag(intent)})


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(min_length=1, max_length=1000)


@router.post("/staff/notifications/{notification_id}/retry", status_code=202)
def retry_notification(notification_id: str, body: RetryRequest, request: Request,
                        if_match: str | None = Header(default=None),
                        principal: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    intent = _staff_intent(db, principal, notification_id)
    case_for(db, principal, intent.case_id, write=True)
    check_version(intent, if_match)
    if not _retry_allowed(intent, utcnow()):
        raise ApiError(409, "NOTIFICATION_RETRY_NOT_ALLOWED", "此通知不可重試；未知結果或逾時請先人工核對。")
    job = db.scalar(select(OutboxJob).where(OutboxJob.intent_id == intent.id).with_for_update())
    if job and job.lease_until and job.lease_until > utcnow():
        raise ApiError(409, "NOTIFICATION_IN_PROGRESS", "通知正在處理，請稍後查詢。")
    if not job:
        job = OutboxJob(intent_id=intent.id)
        db.add(job)
    if (job.attempt_count or 0) >= 5:
        raise ApiError(409, "NOTIFICATION_RETRY_LIMIT", "已達重試次數上限，請交由維運人員核對。")
    job.available_at = utcnow()
    job.lease_owner, job.lease_until = None, None
    intent.status = "READY"
    audit(db, request, actor_id=principal.account.id, action="NOTIFICATION_RETRY_REQUESTED",
          resource_id=intent.id, case_id=intent.case_id, details={"reason": body.reason})
    db.commit()
    return ok(request, public_notification(intent), status_code=202, headers={"ETag": etag(intent)})


@router.post("/webhooks/line")
async def line_webhook(request: Request, x_line_signature: str = Header(default=""),
                       db: Session = Depends(get_db)):
    settings = request.app.state.settings
    received_at = utcnow()
    if not (settings.line_channel_secret and settings.line_destination_user_id and settings.line_messaging_channel_id):
        raise ApiError(503, "LINE_WEBHOOK_NOT_CONFIGURED", "LINE webhook 尚未設定。")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 1024 * 1024:
            raise ApiError(413, "WEBHOOK_TOO_LARGE", "Webhook 本文超過限制。")
    if not verify_signature(bytes(raw), x_line_signature, settings.line_channel_secret):
        raise ApiError(401, "INVALID_WEBHOOK_SIGNATURE", "Webhook 簽章驗證失敗。")
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ApiError(400, "INVALID_WEBHOOK_BODY", "Webhook 格式錯誤。") from exc
    if not isinstance(document, dict) or document.get("destination") != settings.line_destination_user_id:
        raise ApiError(400, "WEBHOOK_DESTINATION_MISMATCH", "Webhook 目的帳號不符。")
    events = document.get("events")
    if not isinstance(events, list) or len(events) > 1000:
        raise ApiError(400, "INVALID_WEBHOOK_EVENTS", "Webhook 事件格式錯誤。")
    replies_enabled = (getattr(settings, "line_bot_enabled", False)
                       and getattr(settings, "line_reply_mode", "disabled") in {"fake", "live"})
    reply_buffer = getattr(request.app.state, "line_reply_buffer", None)
    if replies_enabled and not isinstance(reply_buffer, LineReplyBuffer):
        raise ApiError(503, "LINE_REPLY_WORKER_UNAVAILABLE", "LINE 回覆服務尚未就緒。")
    validated = []
    for event in events:
        if not isinstance(event, dict):
            raise ApiError(400, "INVALID_WEBHOOK_EVENT", "Webhook 事件格式錯誤。")
        event_id, event_type, timestamp = event.get("webhookEventId"), event.get("type"), event.get("timestamp")
        source = event.get("source", {})
        if (not isinstance(event_id, str) or not 1 <= len(event_id) <= 100
                or not isinstance(event_type, str) or not 1 <= len(event_type) <= 100
                or type(timestamp) is not int or timestamp < 0 or not isinstance(source, dict)):
            raise ApiError(400, "INVALID_WEBHOOK_EVENT", "Webhook 事件必要欄位不符。")
        user_id = source.get("userId")
        if user_id is not None and (not isinstance(user_id, str) or len(user_id) > 100):
            raise ApiError(400, "INVALID_WEBHOOK_EVENT", "Webhook 來源格式錯誤。")
        try:
            event_at = datetime.fromtimestamp(timestamp / 1000, UTC)
        except (ValueError, OverflowError, OSError) as exc:
            raise ApiError(400, "INVALID_WEBHOOK_EVENT", "Webhook 時間格式錯誤。") from exc
        # Existing follow association semantics require the subject. Anonymous
        # messages and postbacks never persist a subject, text, token or answers.
        payload = {"user_id": user_id} if event_type in {"follow", "unfollow"} else {}
        validated.append((event_id, event_type, event_at, payload, event))
    inserted = 0
    queued_ids = []
    try:
        for event_id, event_type, event_at, payload, event in validated:
            try:
                with db.begin_nested():
                    db.add(WebhookInbox(channel_id=settings.line_messaging_channel_id, event_id=event_id,
                                        event_type=event_type, event_at=event_at, payload=payload, status="PENDING"))
                    db.flush()
                    if replies_enabled:
                        reply = enqueue_line_reply(db, event, settings, buffer=reply_buffer,
                                                   received_at=received_at, event_at=event_at)
                        if reply is not None:
                            queued_ids.append(reply.id)
                inserted += 1
            except IntegrityError:
                # The unique inbox key, not an in-memory cache, handles redelivery.
                duplicate = db.scalar(select(WebhookInbox.id).where(
                    WebhookInbox.channel_id == settings.line_messaging_channel_id,
                    WebhookInbox.event_id == event_id,
                ))
                if not duplicate:
                    raise
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        for job_id in queued_ids:
            reply_buffer.discard(job_id)
        raise ApiError(503, "WEBHOOK_STORAGE_UNAVAILABLE", "事件尚未可靠保存，請稍後重送。") from exc
    return ok(request, {"accepted": True, "new_events": inserted})
