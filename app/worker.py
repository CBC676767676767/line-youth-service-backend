"""Recoverable, bounded background jobs for files, notifications, and inboxes.

Run one pass with ``python -m app.worker --once``. No external service is
impersonated: missing LINE credentials or unavailable ClamAV fail closed.
"""

from __future__ import annotations

import argparse
import hashlib
import random
import time
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import or_, select, update

from app.common import audit
from app.db import make_engine, make_session_factory, utcnow
from app.models import (
    Account, Case, CaseAccess, File, FileVersion, IdentityLink, NotificationAttempt,
    NotificationIntent, OutboxJob, Task, WebhookInbox,
)
from app.notification_delivery import (
    ACTIONABLE_PURPOSES, decrypt_snapshot, encrypt_snapshot, line_payload, send_line,
)
from app.storage import StorageError, inspect_object, private_path, scan_object


MAX_ATTEMPTS = 5
RETRY_WINDOW = timedelta(hours=24, seconds=-30)


def process_scans(factory, settings, limit=20):
    processed = 0
    now = utcnow()
    with factory() as db:
        # A process may die during its last permitted scan. Expired leases must
        # still reach a visible failure state instead of remaining SCANNING.
        db.execute(update(FileVersion).where(
            FileVersion.scan_status == "SCANNING",
            FileVersion.scan_attempts >= MAX_ATTEMPTS,
            FileVersion.scan_lease_until <= now,
        ).values(scan_status="SCAN_FAILED", scan_lease_until=None, scan_retry_at=None,
                 scanned_at=now, version=FileVersion.version + 1))
        db.execute(update(FileVersion).where(
            FileVersion.scan_status == "PENDING_UPLOAD",
            FileVersion.uploaded_at.is_(None), FileVersion.upload_expires_at <= now,
        ).values(scan_status="EXPIRED", upload_token_digest=None, version=FileVersion.version + 1))
        db.commit()
        candidates = db.scalars(select(FileVersion.id).where(
            FileVersion.uploaded_at.is_not(None),
            FileVersion.scan_status.in_(["PENDING_SCAN", "SCANNING", "SCAN_FAILED"]),
            FileVersion.scan_attempts < MAX_ATTEMPTS,
            or_(FileVersion.scan_retry_at.is_(None), FileVersion.scan_retry_at <= now),
            or_(FileVersion.scan_lease_until.is_(None), FileVersion.scan_lease_until <= now),
        ).order_by(FileVersion.created_at).limit(limit)).all()
    for version_id in candidates:
        with factory() as db:
            lease = utcnow() + timedelta(seconds=max(120, settings.clamav_timeout * 4))
            claimed = db.execute(update(FileVersion).where(
                FileVersion.id == version_id,
                FileVersion.scan_status.in_(["PENDING_SCAN", "SCANNING", "SCAN_FAILED"]),
                FileVersion.scan_attempts < MAX_ATTEMPTS,
                or_(FileVersion.scan_lease_until.is_(None), FileVersion.scan_lease_until <= utcnow()),
            ).values(scan_status="SCANNING", scan_lease_until=lease,
                     scan_attempts=FileVersion.scan_attempts + 1,
                     version=FileVersion.version + 1))
            db.commit()
            if not claimed.rowcount:
                continue
            row = db.get(FileVersion, version_id)
            attempt = row.scan_attempts
            key, expected_size, expected_hash = row.object_key, row.size_bytes, row.sha256
        from app.storage import ScanResult
        try:
            size, digest, _ = inspect_object(settings.storage_dir, key)
            if size != expected_size or digest != expected_hash:
                result = ScanResult("REJECTED", "integrity-check", "FILE_INTEGRITY_ERROR")
            else:
                result = scan_object(private_path(settings.storage_dir, key), settings)
        except (StorageError, OSError):
            result = ScanResult("SCAN_FAILED", "integrity-check", "OBJECT_UNAVAILABLE")
        with factory() as db:
            row = db.scalar(select(FileVersion).where(FileVersion.id == version_id).with_for_update())
            if row.scan_attempts != attempt or row.scan_lease_until != lease or row.scan_status != "SCANNING":
                continue
            row.scan_status, row.scan_engine_version = result.status, result.engine
            row.scanned_at, row.scan_lease_until = utcnow(), None
            row.scan_retry_at = (
                utcnow() + timedelta(seconds=min(3600, 30 * 2 ** attempt))
                if result.status == "SCAN_FAILED" and attempt < MAX_ATTEMPTS else None
            )
            file = db.get(File, row.file_id)
            audit(db, None, actor_id=None, action="FILE_SCAN_COMPLETED", resource_id=row.id,
                  case_id=file.case_id, details={"status": result.status, "engine": result.engine,
                                                 "error_code": result.error, "attempt": attempt})
            db.commit()
            processed += 1
    return processed


def _notifiable(db, intent):
    account = db.get(Account, intent.account_id)
    if not account or account.status != "ACTIVE":
        return "ACCOUNT_INACTIVE"
    access = db.scalar(select(CaseAccess.id).where(
        CaseAccess.account_id == intent.account_id, CaseAccess.case_id == intent.case_id,
        CaseAccess.revoked_at.is_(None),
        or_(CaseAccess.expires_at.is_(None), CaseAccess.expires_at > utcnow()),
    ))
    if not access:
        return "CASE_ACCESS_REVOKED"
    case = db.get(Case, intent.case_id)
    if not case:
        return "CASE_UNAVAILABLE"
    if intent.purpose in ACTIONABLE_PURPOSES:
        task = db.get(Task, intent.task_id) if intent.task_id else None
        if case.status not in {"RECEIVED", "UNDER_REVIEW"}:
            return "CASE_NO_ACTION_REQUIRED"
        if not task or task.status not in {"OPEN", "REOPENED"}:
            return "TASK_NO_ACTION_REQUIRED"
        if task.current_revision_no != intent.task_revision:
            return "TASK_REVISION_CHANGED"
    return None


def _active_link(db, intent):
    return db.scalar(select(IdentityLink).where(
        IdentityLink.account_id == intent.account_id,
        IdentityLink.revoked_at.is_(None),
        IdentityLink.binding_version == intent.binding_version,
    ))


def process_notifications(factory, settings, limit=20, *, transport=None):
    processed = 0
    owner = str(uuid4())
    now = utcnow()
    with factory() as db:
        candidates = db.scalars(select(OutboxJob.id).join(
            NotificationIntent, OutboxJob.intent_id == NotificationIntent.id
        ).where(
            NotificationIntent.status.in_(["PLANNED", "READY", "RETRY_WAIT", "PROCESSING"]),
            OutboxJob.available_at <= now,
            or_(OutboxJob.lease_until.is_(None), OutboxJob.lease_until <= now),
        ).order_by(OutboxJob.available_at).limit(limit)).all()
    for job_id in candidates:
        with factory() as db:
            claim = db.execute(update(OutboxJob).where(
                OutboxJob.id == job_id,
                or_(OutboxJob.lease_until.is_(None), OutboxJob.lease_until <= utcnow()),
                OutboxJob.available_at <= utcnow(),
            ).values(lease_owner=owner, lease_until=utcnow() + timedelta(seconds=120),
                     version=OutboxJob.version + 1))
            db.commit()
            if not claim.rowcount:
                continue
            job = db.get(OutboxJob, job_id)
            intent = db.get(NotificationIntent, job.intent_id)
            if intent.status not in {"PLANNED", "READY", "RETRY_WAIT", "PROCESSING"}:
                job.lease_owner, job.lease_until = None, None
                db.commit()
                continue
            reason = _notifiable(db, intent)
            link = _active_link(db, intent) if intent.channel == "LINE" else None
            if intent.channel == "LINE" and not link:
                reason = "BINDING_REVOKED_OR_CHANGED"
            if reason:
                intent.status, intent.last_error_code = "SKIPPED", reason
                job.lease_owner, job.lease_until = None, None
                db.commit()
                processed += 1
                continue
            if intent.channel == "IN_APP":
                intent.status = "API_ACCEPTED"
                job.attempt_count += 1
                job.lease_owner, job.lease_until = None, None
                db.add(NotificationAttempt(intent_id=intent.id, attempt_no=job.attempt_count,
                                           payload_snapshot={}, result="IN_APP_PUBLISHED"))
                db.commit()
                processed += 1
                continue
            if intent.channel != "LINE" or not settings.line_channel_access_token:
                intent.status = "DEAD_LETTER"
                intent.last_error_code = "LINE_NOT_CONFIGURED" if intent.channel == "LINE" else "CHANNEL_NOT_CONFIGURED"
                job.last_error_code = intent.last_error_code
                job.lease_owner, job.lease_until = None, None
                db.commit()
                processed += 1
                continue
            if intent.first_attempt_at and utcnow() - intent.first_attempt_at >= RETRY_WINDOW:
                intent.status, intent.last_error_code = "UNKNOWN", "RETRY_WINDOW_EXPIRED"
                job.lease_owner, job.lease_until = None, None
                db.commit()
                processed += 1
                continue
            if job.attempt_count >= MAX_ATTEMPTS:
                intent.status, intent.last_error_code = "UNKNOWN", "RETRY_LIMIT_REACHED"
                job.lease_owner, job.lease_until = None, None
                db.commit()
                processed += 1
                continue
            if intent.first_attempt_at is None:
                payload = line_payload(intent, link.line_subject, settings)
                intent.payload_snapshot = encrypt_snapshot(payload, settings.secret_key)
                intent.recipient_snapshot = hashlib.sha256(link.line_subject.encode()).hexdigest()
                intent.retry_key = str(uuid4())
                intent.first_attempt_at = utcnow()
            else:
                try:
                    payload = decrypt_snapshot(intent.payload_snapshot, settings.secret_key)
                except ValueError:
                    intent.status, intent.last_error_code = "UNKNOWN", "SNAPSHOT_UNAVAILABLE"
                    job.lease_owner, job.lease_until = None, None
                    db.commit()
                    continue
                if payload.get("to") != link.line_subject:
                    intent.status, intent.last_error_code = "SKIPPED", "RECIPIENT_CHANGED"
                    job.lease_owner, job.lease_until = None, None
                    db.commit()
                    continue
            intent.status = "PROCESSING"
            # A reclaimed lease means the previous call may have reached LINE.
            # Preserve that uncertainty before issuing the same frozen request.
            for previous in db.scalars(select(NotificationAttempt).where(
                NotificationAttempt.intent_id == intent.id,
                NotificationAttempt.result == "IN_FLIGHT",
            )):
                previous.result = "UNKNOWN"
            job.attempt_count += 1
            attempt = NotificationAttempt(
                id=str(uuid4()), intent_id=intent.id, attempt_no=job.attempt_count,
                retry_key=intent.retry_key, payload_snapshot=intent.payload_snapshot,
                first_attempt_at=intent.first_attempt_at, result="IN_FLIGHT",
            )
            db.add(attempt)
            # Freeze key, destination and encrypted payload durably BEFORE IO.
            db.commit()
            retry_key, attempt_id = intent.retry_key, attempt.id
        outcome = send_line(payload, retry_key, settings, transport=transport)
        with factory() as db:
            job = db.scalar(select(OutboxJob).where(OutboxJob.id == job_id).with_for_update())
            if job.lease_owner != owner:
                continue
            intent = db.get(NotificationIntent, job.intent_id)
            attempt = db.get(NotificationAttempt, attempt_id)
            attempt.result, attempt.http_status = outcome.result, outcome.http_status
            attempt.provider_request_id = outcome.provider_request_id
            attempt.accepted_request_id = outcome.accepted_request_id
            intent.last_error_code, job.last_error_code = outcome.error_code, outcome.error_code
            if outcome.result == "API_ACCEPTED":
                intent.status = "API_ACCEPTED"
            elif outcome.retryable and job.attempt_count < MAX_ATTEMPTS and utcnow() - intent.first_attempt_at < RETRY_WINDOW:
                intent.status = "RETRY_WAIT"
                job.available_at = utcnow() + timedelta(seconds=min(3600, 5 * 2 ** job.attempt_count) + random.uniform(0, 3))
            else:
                intent.status = "UNKNOWN" if outcome.uncertain else "DEAD_LETTER"
            job.lease_owner, job.lease_until = None, None
            db.commit()
            processed += 1
    return processed


def process_inbox(factory, settings, limit=100):
    processed = 0
    with factory() as db:
        rows = db.scalars(select(WebhookInbox).where(
            WebhookInbox.status.in_(["PENDING", "WAITING_ASSOCIATION"]),
            WebhookInbox.channel_id == settings.line_messaging_channel_id,
        ).order_by(WebhookInbox.event_at, WebhookInbox.id).limit(limit).with_for_update(skip_locked=True)).all()
        for row in rows:
            if row.event_type not in {"follow", "unfollow"}:
                row.status, row.processed_at = "IGNORED", utcnow()
                processed += 1
                continue
            subject = row.payload.get("user_id")
            link = db.scalar(select(IdentityLink).where(
                IdentityLink.line_subject == subject,
                IdentityLink.provider_id == settings.line_provider_id,
                IdentityLink.revoked_at.is_(None),
            ).with_for_update()) if subject else None
            if not link:
                row.status = "WAITING_ASSOCIATION"
                continue
            observed = "FOLLOWING" if row.event_type == "follow" else "UNFOLLOWED"
            if link.last_event_at and row.event_at < link.last_event_at:
                row.status = "SKIPPED_OLDER"
            elif link.last_event_at == row.event_at and link.observed_follow_status != observed:
                row.status = "AMBIGUOUS"
            else:
                link.observed_follow_status, link.last_event_at = observed, row.event_at
                row.status = "PROCESSED"
            row.processed_at = utcnow()
            processed += 1
        db.commit()
    return processed


def run_once(session_factory, settings):
    result = {
        "scans": process_scans(session_factory, settings),
        "webhooks": process_inbox(session_factory, settings),
        "notifications": process_notifications(session_factory, settings),
    }
    from app.admin import process_exports
    result["exports"] = process_exports(session_factory, settings)
    return result


def main():
    from app.config import Settings
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    factory = make_session_factory(make_engine(settings.database_url))
    while True:
        print(run_once(factory, settings), flush=True)
        if args.once:
            break
        time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
