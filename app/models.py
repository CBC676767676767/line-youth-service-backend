from datetime import datetime

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime, utcnow


class Account(Base):
    __tablename__ = "accounts"
    email: Mapped[str] = mapped_column(String(254), unique=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    role_version: Mapped[int] = mapped_column(default=1)
    password_hash: Mapped[str | None] = mapped_column(Text)
    totp_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    last_totp_step: Mapped[int | None] = mapped_column(Integer)

    @property
    def is_active(self):
        return self.status == "ACTIVE"


class RoleGrant(Base):
    __tablename__ = "role_grants"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    scope_type: Mapped[str] = mapped_column(String(20), default="SCHEME")
    scope_id: Mapped[str | None] = mapped_column(String(36))
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AuthSession(Base):
    __tablename__ = "sessions"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    auth_method: Mapped[str] = mapped_column(String(30))
    role_version: Mapped[int] = mapped_column(default=1)
    reauth_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class EmailChallenge(Base):
    __tablename__ = "email_challenges"
    email: Mapped[str] = mapped_column(String(254), index=True)
    purpose: Mapped[str] = mapped_column(String(30), default="LOGIN")
    code_hash: Mapped[str] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(default=0)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    request_ip_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"))
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id"))


class MfaChallenge(Base):
    __tablename__ = "mfa_challenges"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    attempts: Mapped[int] = mapped_column(default=0)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class LineChallenge(Base):
    __tablename__ = "line_challenges"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class IdentityLink(Base):
    __tablename__ = "identity_links"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    provider_id: Mapped[str] = mapped_column(String(100))
    line_subject: Mapped[str] = mapped_column(String(100))
    binding_version: Mapped[int] = mapped_column(default=1)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    observed_follow_status: Mapped[str | None] = mapped_column(String(20))
    last_event_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    __table_args__ = (
        Index("uq_active_line_subject", "provider_id", "line_subject", unique=True,
              sqlite_where=text("revoked_at IS NULL"), postgresql_where=text("revoked_at IS NULL")),
        Index("uq_active_line_account", "account_id", unique=True,
              sqlite_where=text("revoked_at IS NULL"), postgresql_where=text("revoked_at IS NULL")),
    )


class Scheme(Base):
    __tablename__ = "schemes"
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    schema_version: Mapped[int] = mapped_column(default=1)
    form_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Case(Base):
    __tablename__ = "cases"
    scheme_id: Mapped[str] = mapped_column(ForeignKey("schemes.id"), index=True)
    case_no: Mapped[str] = mapped_column(String(50), unique=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    assigned_to: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", index=True)
    form_data: Mapped[dict] = mapped_column(JSON, default=dict)
    schema_version: Mapped[int] = mapped_column(default=1)
    current_revision_no: Mapped[int] = mapped_column(default=0)
    # Bumped when accepted evidence changes. A review conclusion records the value
    # it was reached against, so a stale PASS cannot gate a later decision.
    evidence_revision_no: Mapped[int] = mapped_column(default=0)
    last_business_update_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class CaseAccess(Base):
    __tablename__ = "case_access"
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    permission: Mapped[str] = mapped_column(String(20), default="OWNER")
    grant_source: Mapped[str] = mapped_column(String(30), default="CREATED")
    verification_id: Mapped[str | None] = mapped_column(String(100))
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class CaseRevision(Base):
    __tablename__ = "case_revisions"
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    revision_no: Mapped[int] = mapped_column()
    schema_version: Mapped[int] = mapped_column()
    form_snapshot: Mapped[dict] = mapped_column(JSON)
    submitted_by: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    submitted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("case_id", "revision_no"),)


class Task(Base):
    __tablename__ = "tasks"
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="OPEN", index=True)
    current_revision_no: Mapped[int] = mapped_column(default=1)
    title: Mapped[str] = mapped_column(String(200))
    requirement: Mapped[str] = mapped_column(Text)
    acceptance_criteria: Mapped[str] = mapped_column(Text, default="")
    example_ref: Mapped[str | None] = mapped_column(String(500))
    due_at: Mapped[datetime] = mapped_column(UTCDateTime)
    accepted_submission_id: Mapped[str | None] = mapped_column(String(36))
    assigned_to: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"))


class TaskRevision(Base):
    __tablename__ = "task_revisions"
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    revision_no: Mapped[int] = mapped_column()
    title: Mapped[str] = mapped_column(String(200))
    requirement: Mapped[str] = mapped_column(Text)
    acceptance_criteria: Mapped[str] = mapped_column(Text, default="")
    example_ref: Mapped[str | None] = mapped_column(String(500))
    due_at: Mapped[datetime] = mapped_column(UTCDateTime)
    change_reason: Mapped[str] = mapped_column(Text)
    changed_by: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    __table_args__ = (UniqueConstraint("task_id", "revision_no"),)


class Submission(Base):
    __tablename__ = "submissions"
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), index=True)
    task_revision: Mapped[int | None] = mapped_column()
    case_revision_id: Mapped[str | None] = mapped_column(ForeignKey("case_revisions.id"))
    submitted_by: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    submitted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    file_version_ids: Mapped[list] = mapped_column(JSON, default=list)
    statement: Mapped[str | None] = mapped_column(Text)
    deadline_snapshot: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Receipt(Base):
    __tablename__ = "receipts"
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    submission_id: Mapped[str] = mapped_column(ForeignKey("submissions.id"), unique=True)
    receipt_no: Mapped[str] = mapped_column(String(50), unique=True)
    submitted_at: Mapped[datetime] = mapped_column(UTCDateTime)
    snapshot: Mapped[dict] = mapped_column(JSON)


class File(Base):
    __tablename__ = "files"
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))
    created_by: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    document_type: Mapped[str] = mapped_column(String(100), default="OTHER")
    original_name: Mapped[str] = mapped_column(String(255))
    current_version_id: Mapped[str | None] = mapped_column(String(36))


class FileVersion(Base):
    __tablename__ = "file_versions"
    file_id: Mapped[str] = mapped_column(ForeignKey("files.id"), index=True)
    object_key: Mapped[str] = mapped_column(String(200), unique=True)
    size_bytes: Mapped[int] = mapped_column()
    declared_type: Mapped[str] = mapped_column(String(100))
    detected_type: Mapped[str | None] = mapped_column(String(100))
    sha256: Mapped[str | None] = mapped_column(String(64))
    scan_status: Mapped[str] = mapped_column(String(30), default="PENDING_UPLOAD", index=True)
    scan_engine_version: Mapped[str | None] = mapped_column(String(200))
    upload_token_digest: Mapped[str | None] = mapped_column(String(64))
    upload_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    uploaded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    scanned_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    scan_attempts: Mapped[int] = mapped_column(default=0)
    scan_retry_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    scan_lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ReviewItem(Base):
    __tablename__ = "review_items"
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    criterion_code: Mapped[str] = mapped_column(String(100))
    result: Mapped[str] = mapped_column(String(20), default="PENDING")
    internal_note: Mapped[str | None] = mapped_column(Text)
    public_reason: Mapped[str | None] = mapped_column(Text)
    evidence_refs: Mapped[list] = mapped_column(JSON, default=list)
    reviewed_by: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reviewed_evidence_revision: Mapped[int] = mapped_column(default=0)
    __table_args__ = (UniqueConstraint("case_id", "criterion_code"),)


class Decision(Base):
    __tablename__ = "decisions"
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    outcome: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text)
    rule_version_id: Mapped[str] = mapped_column(String(100))
    evidence_snapshot: Mapped[list] = mapped_column(JSON, default=list)
    decided_by: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("decisions.id"))


class DomainEvent(Base):
    __tablename__ = "domain_events"
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(100))
    resource_id: Mapped[str] = mapped_column(String(36))
    resource_version: Mapped[int] = mapped_column(default=1)
    public_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class NotificationIntent(Base):
    __tablename__ = "notification_intents"
    event_id: Mapped[str] = mapped_column(ForeignKey("domain_events.id"))
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))
    task_revision: Mapped[int | None] = mapped_column()
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    binding_version: Mapped[int | None] = mapped_column()
    channel: Mapped[str] = mapped_column(String(20), default="IN_APP")
    purpose: Mapped[str] = mapped_column(String(100))
    scheduled_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(30), default="READY", index=True)
    payload_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    recipient_snapshot: Mapped[str | None] = mapped_column(String(100))
    retry_key: Mapped[str | None] = mapped_column(String(36))
    first_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    viewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    __table_args__ = (UniqueConstraint("event_id", "purpose", "channel", "account_id"),)


class OutboxJob(Base):
    __tablename__ = "outbox_jobs"
    intent_id: Mapped[str] = mapped_column(ForeignKey("notification_intents.id"), unique=True)
    available_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    attempt_count: Mapped[int] = mapped_column(default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(100))


class NotificationAttempt(Base):
    __tablename__ = "notification_attempts"
    intent_id: Mapped[str] = mapped_column(ForeignKey("notification_intents.id"), index=True)
    attempt_no: Mapped[int] = mapped_column()
    retry_key: Mapped[str | None] = mapped_column(String(36))
    payload_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    first_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    attempted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    http_status: Mapped[int | None] = mapped_column()
    provider_request_id: Mapped[str | None] = mapped_column(String(100))
    accepted_request_id: Mapped[str | None] = mapped_column(String(100))
    result: Mapped[str] = mapped_column(String(30))


class WebhookInbox(Base):
    __tablename__ = "webhook_inbox"
    channel_id: Mapped[str] = mapped_column(String(100))
    event_id: Mapped[str] = mapped_column(String(100))
    event_type: Mapped[str] = mapped_column(String(100))
    event_at: Mapped[datetime] = mapped_column(UTCDateTime)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="PENDING")
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    __table_args__ = (UniqueConstraint("channel_id", "event_id"),)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    method: Mapped[str] = mapped_column(String(10))
    route_scope: Mapped[str] = mapped_column(String(300))
    key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="RUNNING")
    response_data: Mapped[dict | list | None] = mapped_column(JSON)
    response_status: Mapped[int] = mapped_column(default=200)
    response_headers: Mapped[dict] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    __table_args__ = (UniqueConstraint("account_id", "method", "route_scope", "key"),)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"))
    action: Mapped[str] = mapped_column(String(100))
    resource_id: Mapped[str | None] = mapped_column(String(100))
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id"), index=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    request_id: Mapped[str] = mapped_column(String(100))


class ExportJob(Base):
    __tablename__ = "export_jobs"
    requested_by: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    filter_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    column_set: Mapped[list] = mapped_column(JSON, default=list)
    purpose: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="PENDING")
    object_key: Mapped[str | None] = mapped_column(String(200))
    case_ids: Mapped[list] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ClaimRequest(Base):
    __tablename__ = "claim_requests"
    requested_by: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    reference: Mapped[str | None] = mapped_column(Text)
    contact_note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="PENDING")
    verification_id: Mapped[str | None] = mapped_column(String(200))
    resolved_case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id"))
    resolved_by: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"))
    public_message: Mapped[str | None] = mapped_column(Text)


class ContentVersion(Base):
    __tablename__ = "content_versions"
    kind: Mapped[str] = mapped_column(String(30))
    code: Mapped[str] = mapped_column(String(100))
    version_no: Mapped[int] = mapped_column()
    body: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT")
    approved_by: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"))
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    __table_args__ = (UniqueConstraint("kind", "code", "version_no"),)
