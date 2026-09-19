"""Transactional case, task, review, and immutable receipt workflows."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.access import case_filter, case_for
from app.auth import Principal, current_principal
from app.common import ApiError, check_version, encode, etag, idem_finish, idem_start, ok
from app.db import get_db, new_id, utcnow
from app.files import file_metadata
from app.models import (
    Account, AuditEvent, Case, CaseAccess, CaseRevision, ContentVersion, Decision,
    DomainEvent, File, FileVersion, IdempotencyRecord, NotificationIntent,
    Receipt, ReviewItem, RoleGrant, Scheme, Submission, Task, TaskRevision,
)
from app.notifications import enqueue_notification
from app.schemas_cases import (
    BatchAssignment, CaseClose, CaseCreate, DecisionCreate, DraftPatch,
    OperationLookup, Reason, ReviewUpdate, SubmitCase, TaskAccept, TaskCreate,
    TaskReopen, TaskRevise, TaskSubmission,
)

router = APIRouter(tags=["cases"])
DB = Annotated[Session, Depends(get_db)]
User = Annotated[Principal, Depends(current_principal)]
VersionHeader = Annotated[str | None, Header(alias="If-Match")]
CASE_STATES = {"DRAFT", "RECEIVED", "UNDER_REVIEW", "DECIDED", "CLOSED", "WITHDRAWN"}
TASK_STATES = {"OPEN", "SUBMITTED", "ACCEPTED", "REOPENED", "CANCELLED"}
SUBMIT_FILE_STATES = {"PENDING_SCAN", "SCANNING", "SCAN_FAILED", "CLEAN"}
ACTIONABLE_PURPOSES = {"TASK_CREATED", "TASK_REVISED", "TASK_REOPENED", "TASK_REMINDER"}


def _require_role(p: Principal, *roles: str):
    if not p.roles.intersection(roles):
        raise ApiError(403, "FORBIDDEN", "此帳號沒有執行此操作的角色。")


def _has_scope(p: Principal, scheme_id: str, roles: set[str], case_id=None) -> bool:
    now = utcnow()
    return any(g.role in roles and g.revoked_at is None and
               (g.expires_at is None or g.expires_at > now) and
               (g.scope_type == "GLOBAL" or
                (g.scope_type == "SCHEME" and g.scope_id == scheme_id) or
                (g.scope_type == "CASE" and g.scope_id == case_id)) for g in p.grants)


def _staff_case(db: Session, p: Principal, case_id: str, *, write=False, supervisor=False):
    roles = {"supervisor"} if supervisor else {"reviewer", "supervisor"}
    if not write and not supervisor:
        roles.add("auditor")
    _require_role(p, *roles)
    case = case_for(db, p, case_id, write=write)
    if not _has_scope(p, case.scheme_id, roles, case.id):
        raise ApiError(404, "RESOURCE_NOT_FOUND", "找不到可存取的案件。")
    return case


def _applicant_case(db: Session, p: Principal, case_id: str, *, write=False):
    _require_role(p, "applicant")
    case = case_for(db, p, case_id, write=write)
    now = utcnow()
    grant = db.scalar(select(CaseAccess).where(
        CaseAccess.case_id == case_id, CaseAccess.account_id == p.account.id,
        CaseAccess.revoked_at.is_(None),
        or_(CaseAccess.expires_at.is_(None), CaseAccess.expires_at > now),
        CaseAccess.permission.in_(["OWNER", "WRITE"] if write else ["OWNER", "WRITE", "READ"]),
    ))
    if not grant:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "找不到可存取的案件。")
    return case


def _audit(db: Session, p: Principal, request: Request, action: str, case: Case, resource_id=None,
           details=None):
    db.add(AuditEvent(actor_id=p.account.id, action=action, resource_id=resource_id or case.id,
                      case_id=case.id, details=details or {},
                      request_id=getattr(request.state, "request_id", new_id())))


def _event(db: Session, case: Case, event_type: str, *, task: Task | None = None,
           payload=None, notify=True):
    case.last_business_update_at = utcnow()
    db.flush()
    event = DomainEvent(id=new_id(), case_id=case.id, event_type=event_type,
                        resource_id=task.id if task else case.id,
                        resource_version=task.version if task else case.version,
                        public_payload=payload or {})
    db.add(event)
    db.flush()
    if notify:
        now = utcnow()
        owners = db.scalars(select(CaseAccess.account_id).where(
            CaseAccess.case_id == case.id, CaseAccess.permission == "OWNER",
            CaseAccess.revoked_at.is_(None),
            or_(CaseAccess.expires_at.is_(None), CaseAccess.expires_at > now),
        ).distinct()).all()
        for account_id in owners:
            enqueue_notification(
                db, event_id=event.id, case_id=case.id, account_id=account_id,
                purpose=event_type, task_id=task.id if task else None,
                task_revision=task.current_revision_no if task else None,
                payload={"text": "您的案件有新進度，請登入查看。",
                         "path": f"/tasks/{task.id}" if task else f"/cases/{case.id}"},
            )
    return event


def _cancel_task_reminders(db: Session, case_id: str, task_id: str | None = None):
    query = select(NotificationIntent).where(
        NotificationIntent.case_id == case_id,
        NotificationIntent.purpose.in_(ACTIONABLE_PURPOSES),
        NotificationIntent.status.in_(["PLANNED", "READY", "RETRY_WAIT"]),
    )
    if task_id:
        query = query.where(NotificationIntent.task_id == task_id)
    for intent in db.scalars(query):
        intent.status = "CANCELLED"
        intent.last_error_code = "TASK_CHANGED"


def _scheme(db: Session, scheme_id: str, *, active=False):
    scheme = db.get(Scheme, scheme_id)
    if not scheme or (active and not scheme.active):
        raise ApiError(404, "SCHEME_NOT_FOUND", "找不到目前可申請的方案。")
    return scheme


def _scheme_contract(scheme: Scheme, db: Session):
    rule = db.get(ContentVersion, scheme.config.get("rule_version_id")) if scheme.config.get("rule_version_id") else None
    rule_id = (rule.id if rule and rule.kind == "RULE" and rule.code == scheme.id
               and rule.status == "PUBLISHED" else None)
    return {"rule_version_id": rule_id,
            "required_criteria": scheme.config.get("required_criteria", []),
            "criterion_labels": scheme.config.get("criterion_labels", {}),
            "required_document_types": scheme.config.get("required_document_types", []),
            "conditional_document_requirements": scheme.config.get("conditional_document_requirements", [])}


def _public_scheme(scheme: Scheme, db: Session):
    return {"id": scheme.id, "name": scheme.name, "description": scheme.description,
            "active": scheme.active, "schema_version": scheme.schema_version,
            "document_types": scheme.config.get("document_types", []),
            "contact": scheme.config.get("contact", {}), **_scheme_contract(scheme, db)}


def _required_documents(scheme: Scheme, form_data: dict):
    required = set(scheme.config.get("required_document_types", []))
    for condition in scheme.config.get("conditional_document_requirements", []):
        if (condition["field"] in form_data and
                form_data[condition["field"]] == condition["equals"]):
            required.update(condition["document_types"])
    return required


def _validate_form(scheme: Scheme, data: dict, *, draft: bool):
    schema = copy.deepcopy(scheme.form_schema)
    if not schema or schema.get("type") != "object":
        raise ApiError(409, "SCHEMA_NOT_PUBLISHED", "此方案尚未設定可用表單。")
    # A form is always a fixed allowlist, even when its schema omits this keyword.
    schema["additionalProperties"] = False
    if not draft and any(isinstance(data.get(name), str) and not data[name].strip()
                         for name in schema.get("required", [])):
        raise ApiError(422, "FORM_INVALID", "必填文字欄位不可僅填寫空白。")
    if draft:
        def relax_required(node):
            if isinstance(node, dict):
                node.pop("required", None)
                for value in node.values():
                    relax_required(value)
            elif isinstance(node, list):
                for value in node:
                    relax_required(value)
        relax_required(schema)
        if scheme.config.get("allow_empty_draft_fields", False):
            for key, field in schema.get("properties", {}).items():
                if field.get("type") == "string":
                    schema["properties"][key] = {"anyOf": [field, {"const": ""}]}
    errors = sorted(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(data),
                    key=lambda item: str(list(item.path)))
    if errors:
        raise ApiError(422, "FORM_INVALID", "請確認表單欄位。", [
            {"field": ".".join(str(part) for part in error.path),
             "message": "欄位不符合方案格式、必填或長度要求。"} for error in errors[:20]
        ])


def _permission(db: Session, p: Principal, case_id: str):
    return set(db.scalars(select(CaseAccess.permission).where(
        CaseAccess.case_id == case_id, CaseAccess.account_id == p.account.id,
        CaseAccess.revoked_at.is_(None),
        or_(CaseAccess.expires_at.is_(None), CaseAccess.expires_at > utcnow()),
    )))


def _case_view(case: Case, p: Principal, *, staff=False, db: Session | None = None):
    actions = []
    if "applicant" in p.roles:
        if case.status == "DRAFT":
            actions.extend(["save_draft", "submit_case", "upload_file"])
        if case.status in {"DRAFT", "RECEIVED", "UNDER_REVIEW"}:
            actions.append("withdraw_case")
    if staff:
        actions = []
        if case.status == "RECEIVED" and p.roles.intersection({"reviewer", "supervisor"}):
            actions.append("start_review")
        if case.status == "UNDER_REVIEW" and p.roles.intersection({"reviewer", "supervisor"}):
            actions.extend(["create_task", "review_case"])
        if "supervisor" in p.roles and _has_scope(p, case.scheme_id, {"supervisor"}, case.id):
            if case.status == "UNDER_REVIEW":
                actions.append("create_decision")
            if case.status == "DECIDED":
                actions.append("close_case")
    if db is not None:
        if staff:
            if not db.scalar(select(Case.id).where(Case.id == case.id, case_filter(p, write=True))):
                actions = []
        else:
            permission = _permission(db, p, case.id)
            if not permission.intersection({"OWNER", "WRITE"}):
                actions = []
            elif "OWNER" not in permission:
                actions = [action for action in actions if action != "upload_file"]
            scheme = _scheme(db, case.scheme_id)
            if not scheme.config.get("allow_withdrawal", True):
                actions = [action for action in actions if action != "withdraw_case"]
            if not scheme.active:
                actions = [action for action in actions if action != "submit_case"]
    value = {"id": case.id, "case_no": case.case_no, "scheme_id": case.scheme_id,
             "status": case.status, "form_data": case.form_data,
             "schema_version": case.schema_version, "current_revision_no": case.current_revision_no,
             "last_business_update_at": case.last_business_update_at,
             "version": case.version, "etag": etag(case), "allowed_actions": actions}
    if staff:
        value["assigned_to"] = case.assigned_to
    return value


def _task_view(task: Task, *, staff=False, db: Session | None = None, p: Principal | None = None):
    actions = []
    if task.status in {"OPEN", "REOPENED"}:
        actions = ["upload_file", "submit_task"] if not staff else ["revise_task", "cancel_task"]
    elif task.status == "SUBMITTED" and staff:
        actions = ["accept_submission", "reopen_task", "cancel_task"]
    if db is not None and p is not None:
        case = db.get(Case, task.case_id)
        if case.status != "UNDER_REVIEW":
            actions = []
        elif staff:
            if not db.scalar(select(Case.id).where(Case.id == case.id, case_filter(p, write=True))):
                actions = []
        else:
            permission = _permission(db, p, case.id)
            if not permission.intersection({"OWNER", "WRITE"}):
                actions = []
            elif "OWNER" not in permission:
                actions = [action for action in actions if action != "upload_file"]
            scheme = _scheme(db, case.scheme_id)
            if task.due_at < utcnow() and not scheme.config.get("allow_late_submission", False):
                actions = [action for action in actions if action != "submit_task"]
    return {"id": task.id, "case_id": task.case_id, "status": task.status,
            "title": task.title, "requirement": task.requirement,
            "acceptance_criteria": task.acceptance_criteria, "example_ref": task.example_ref,
            "due_at": task.due_at, "task_revision": task.current_revision_no,
            "is_overdue": task.status in {"OPEN", "REOPENED"} and task.due_at < utcnow(),
            "accepted_submission_id": task.accepted_submission_id,
            "version": task.version, "etag": etag(task), "allowed_actions": actions}


def _page(db: Session, query, model, cursor: str | None, limit: int):
    if cursor:
        previous = db.scalar(query.where(model.id == cursor))
        if previous is None:
            raise ApiError(422, "CURSOR_INVALID", "分頁游標不適用於此查詢。")
        query = query.where(or_(model.created_at > previous.created_at,
                                (model.created_at == previous.created_at) & (model.id > previous.id)))
    rows = db.scalars(query.order_by(model.created_at, model.id).limit(limit + 1)).all()
    return rows[:limit], rows[limit - 1].id if len(rows) > limit else None


def _database_time(db: Session):
    clock = func.clock_timestamp() if db.bind.dialect.name == "postgresql" else func.current_timestamp()
    value = db.scalar(select(clock))
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _files(db: Session, case: Case, ids: list[str], *, task_id=None, clean=False):
    versions = []
    for file_id in ids:
        item = db.get(FileVersion, file_id)
        file = db.get(File, item.file_id) if item else None
        if not item or not file or file.case_id != case.id or file.task_id != task_id:
            raise ApiError(422, "FILE_NOT_ALLOWED", "附件不屬於此案件或此次補件任務。")
        if not item.uploaded_at or not item.sha256 or not item.object_key:
            raise ApiError(422, "FILE_NOT_READY", "附件尚未完成儲存。")
        if clean and item.scan_status != "CLEAN":
            raise ApiError(409, "FILE_NOT_CLEAN", "附件尚未通過檢查，不能接受或核定。")
        if item.scan_status not in SUBMIT_FILE_STATES:
            raise ApiError(422, "FILE_NOT_READY", "附件尚未完成儲存或已被拒絕。")
        versions.append(item)
    return versions


def _case_files(db: Session, case_id: str, *, submitted_only=False):
    """Call only after case authorization. Never disclose private storage keys."""
    query = select(File, FileVersion).join(FileVersion, FileVersion.file_id == File.id).where(
        File.case_id == case_id, FileVersion.uploaded_at.is_not(None),
        FileVersion.sha256.is_not(None), FileVersion.object_key.is_not(None),
    )
    if submitted_only:
        version_ids = {version_id for submission in db.scalars(
            select(Submission).where(Submission.case_id == case_id))
            for version_id in submission.file_version_ids}
        if not version_ids:
            return []
        query = query.where(FileVersion.id.in_(version_ids))
    return [file_metadata(file, version) for file, version in db.execute(
        query.order_by(File.created_at, File.id, FileVersion.created_at, FileVersion.id))]


def _receipt(db: Session, case: Case, p: Principal, body: SubmitCase, submitted_at: datetime,
             *, revision=None, task=None):
    submission = Submission(id=new_id(), case_id=case.id, task_id=task.id if task else None,
                            task_revision=task.current_revision_no if task else None,
                            case_revision_id=revision.id if revision else None,
                            submitted_by=p.account.id, submitted_at=submitted_at,
                            file_version_ids=list(body.file_version_ids),
                            statement=getattr(body, "statement", None),
                            deadline_snapshot=task.due_at if task else None)
    db.add(submission)
    db.flush()
    receipt_id = new_id()
    snapshot = encode({"receipt_id": receipt_id, "case_id": case.id,
                                "submission_id": submission.id,
                                "task_revision": submission.task_revision,
                                "submitted_at": submitted_at,
                                "deadline_snapshot": submission.deadline_snapshot,
                                "file_version_ids": list(body.file_version_ids), "status": "RECEIVED"})
    receipt = Receipt(id=receipt_id, case_id=case.id, submission_id=submission.id,
                      receipt_no="R-" + receipt_id.replace("-", ""),
                      submitted_at=submitted_at, snapshot=snapshot)
    db.add(receipt)
    return receipt


@router.get("/schemes")
def list_schemes(request: Request, db: DB, active: bool = True):
    query = select(Scheme)
    if active:
        query = query.where(Scheme.active.is_(True))
    return ok(request, {"items": [_public_scheme(s, db) for s in db.scalars(query.order_by(Scheme.id))]})


@router.get("/schemes/{scheme_id}")
def get_scheme(scheme_id: str, request: Request, db: DB):
    return ok(request, _public_scheme(_scheme(db, scheme_id), db))


@router.get("/schemes/{scheme_id}/form-schema")
def get_form_schema(scheme_id: str, request: Request, db: DB, p: User,
                    schema_version: int | None = None):
    scheme = _scheme(db, scheme_id)
    if schema_version is not None and schema_version != scheme.schema_version:
        raise ApiError(404, "SCHEMA_VERSION_NOT_FOUND", "目前沒有此表單版本。")
    return ok(request, {"schema_version": scheme.schema_version, "form_schema": scheme.form_schema,
                        "document_types": scheme.config.get("document_types", []),
                        **_scheme_contract(scheme, db)})


@router.post("/cases")
def create_case(body: CaseCreate, request: Request, db: DB, p: User):
    _require_role(p, "applicant")
    record, replay = idem_start(db, p, request, request.url.path, body.model_dump(mode="json"))
    if replay:
        case_for(db, p, record.response_data["id"])
        return replay
    scheme = _scheme(db, body.scheme_id, active=True)
    case = Case(id=new_id(), scheme_id=scheme.id, created_by=p.account.id,
                case_no="Y-" + new_id().replace("-", ""), form_data={},
                schema_version=scheme.schema_version, status="DRAFT")
    db.add(case)
    db.flush()
    db.add(CaseAccess(case_id=case.id, account_id=p.account.id, permission="OWNER", grant_source="CREATED"))
    _audit(db, p, request, "CASE_CREATED", case)
    _event(db, case, "CASE_CREATED", notify=False)
    data = _case_view(case, p, db=db)
    headers = {"ETag": etag(case)}
    idem_finish(db, record, data, status_code=201, headers=headers)
    db.commit()
    return ok(request, data, status_code=201, headers=headers)


@router.get("/cases")
def list_cases(request: Request, db: DB, p: User, status: str | None = None,
               cursor: str | None = None, limit: int = Query(default=20, ge=1, le=100)):
    _require_role(p, "applicant")
    own = select(CaseAccess.case_id).where(
        CaseAccess.account_id == p.account.id, CaseAccess.revoked_at.is_(None),
        or_(CaseAccess.expires_at.is_(None), CaseAccess.expires_at > utcnow()),
    )
    query = select(Case).where(Case.id.in_(own))
    if status:
        if status not in CASE_STATES:
            raise ApiError(422, "STATUS_INVALID", "案件狀態不正確。")
        query = query.where(Case.status == status)
    rows, next_cursor = _page(db, query, Case, cursor, limit)
    return ok(request, {"items": [_case_view(c, p, db=db) for c in rows], "next_cursor": next_cursor})


@router.get("/cases/{case_id}")
def get_case(case_id: str, request: Request, db: DB, p: User):
    case = _applicant_case(db, p, case_id)
    data = _case_view(case, p, db=db)
    data["files"] = _case_files(db, case_id)
    data["tasks"] = [_task_view(t, db=db, p=p) for t in db.scalars(select(Task).where(Task.case_id == case_id))]
    superseded = select(Decision.supersedes_id).where(Decision.supersedes_id.is_not(None))
    decision = db.scalar(select(Decision).where(Decision.case_id == case_id, Decision.id.notin_(superseded)))
    data["decision"] = {"id": decision.id, "outcome": decision.outcome, "reason": decision.reason,
                        "decided_at": decision.decided_at} if decision else None
    return ok(request, data, headers={"ETag": etag(case)})


@router.patch("/cases/{case_id}")
def patch_case(case_id: str, body: DraftPatch, request: Request, db: DB, p: User,
               if_match: VersionHeader = None):
    case = _applicant_case(db, p, case_id, write=True)
    check_version(case, if_match)
    if case.status != "DRAFT":
        raise ApiError(409, "STATE_CONFLICT", "已送件的申請不可直接改寫。")
    scheme = _scheme(db, case.scheme_id)
    if case.schema_version != scheme.schema_version:
        raise ApiError(409, "SCHEMA_VERSION_CHANGED", "方案表單已更新，請洽承辦確認草稿。")
    _validate_form(scheme, body.form_data, draft=True)
    case.form_data = copy.deepcopy(body.form_data)
    _audit(db, p, request, "DRAFT_SAVED", case)
    _event(db, case, "DRAFT_SAVED", notify=False)
    db.commit()
    return ok(request, _case_view(case, p, db=db), headers={"ETag": etag(case)})


@router.post("/cases/{case_id}/submit")
def submit_case(case_id: str, body: SubmitCase, request: Request, db: DB, p: User,
                if_match: VersionHeader = None):
    case = _applicant_case(db, p, case_id, write=True)
    record, replay = idem_start(db, p, request, request.url.path, body.model_dump(mode="json"))
    if replay:
        return replay
    check_version(case, if_match)
    if case.status != "DRAFT":
        raise ApiError(409, "STATE_CONFLICT", "此案件已送件或不允許送件。")
    scheme = _scheme(db, case.scheme_id, active=True)
    if case.schema_version != scheme.schema_version:
        raise ApiError(409, "SCHEMA_VERSION_CHANGED", "方案表單版本已更新。")
    _validate_form(scheme, case.form_data, draft=False)
    files = _files(db, case, body.file_version_ids)
    document_types = {db.get(File, item.file_id).document_type for item in files}
    missing = _required_documents(scheme, case.form_data) - document_types
    if missing:
        raise ApiError(422, "REQUIRED_DOCUMENT_MISSING", "尚未提供方案要求的文件。", [
            {"field": "file_version_ids", "message": f"缺少文件類別：{document_type}"}
            for document_type in sorted(missing)
        ])
    now = _database_time(db)
    case.current_revision_no += 1
    revision = CaseRevision(id=new_id(), case_id=case.id, revision_no=case.current_revision_no,
                            schema_version=case.schema_version, form_snapshot=copy.deepcopy(case.form_data),
                            submitted_by=p.account.id, submitted_at=now)
    db.add(revision)
    db.flush()
    receipt = _receipt(db, case, p, body, now, revision=revision)
    case.status = "RECEIVED"
    _audit(db, p, request, "CASE_SUBMITTED", case, details={"receipt_id": receipt.id})
    _event(db, case, "CASE_SUBMITTED", payload={"receipt_id": receipt.id})
    data = {"case_id": case.id, "case_status": case.status, "version": case.version,
            "receipt": receipt.snapshot, "allowed_actions": _case_view(case, p, db=db)["allowed_actions"]}
    headers = {"ETag": etag(case)}
    idem_finish(db, record, data, status_code=201, headers=headers)
    db.commit()
    return ok(request, data, status_code=201, headers=headers)


@router.post("/cases/{case_id}/withdraw")
def withdraw_case(case_id: str, body: Reason, request: Request, db: DB, p: User,
                  if_match: VersionHeader = None):
    case = _applicant_case(db, p, case_id, write=True)
    check_version(case, if_match)
    if case.status not in {"DRAFT", "RECEIVED", "UNDER_REVIEW"}:
        raise ApiError(409, "WITHDRAW_NOT_ALLOWED", "此案件目前不可撤回。")
    if not _scheme(db, case.scheme_id).config.get("allow_withdrawal", True):
        raise ApiError(409, "WITHDRAW_NOT_ALLOWED", "此方案須經承辦程序撤回。")
    for task in db.scalars(select(Task).where(Task.case_id == case.id,
                                             Task.status.in_(["OPEN", "REOPENED", "SUBMITTED"]))):
        task.status = "CANCELLED"
    _cancel_task_reminders(db, case.id)
    case.status = "WITHDRAWN"
    _audit(db, p, request, "CASE_WITHDRAWN", case, details={"reason": body.reason})
    _event(db, case, "CASE_WITHDRAWN")
    db.commit()
    return ok(request, _case_view(case, p, db=db), headers={"ETag": etag(case)})


@router.get("/cases/{case_id}/timeline")
def timeline(case_id: str, request: Request, db: DB, p: User,
             cursor: str | None = None, limit: int = Query(default=20, ge=1, le=100)):
    _applicant_case(db, p, case_id)
    rows, next_cursor = _page(db, select(DomainEvent).where(DomainEvent.case_id == case_id),
                             DomainEvent, cursor, limit)
    return ok(request, {"items": [{"id": e.id, "event_type": e.event_type,
                                   "occurred_at": e.occurred_at, "details": e.public_payload}
                                  for e in rows], "next_cursor": next_cursor})


@router.get("/cases/{case_id}/receipts")
def case_receipts(case_id: str, request: Request, db: DB, p: User,
                  cursor: str | None = None, limit: int = Query(default=20, ge=1, le=100)):
    _applicant_case(db, p, case_id)
    rows, next_cursor = _page(db, select(Receipt).where(Receipt.case_id == case_id), Receipt, cursor, limit)
    return ok(request, {"items": [r.snapshot for r in rows], "next_cursor": next_cursor})


@router.get("/receipts/{receipt_id}")
def get_receipt(receipt_id: str, request: Request, db: DB, p: User):
    receipt = db.get(Receipt, receipt_id)
    if not receipt:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "找不到回執。")
    _applicant_case(db, p, receipt.case_id)
    return ok(request, receipt.snapshot)


@router.post("/operations/lookup")
def lookup_operation(body: OperationLookup, request: Request, db: DB, p: User):
    record = db.scalar(select(IdempotencyRecord).where(
        IdempotencyRecord.account_id == p.account.id, IdempotencyRecord.key == body.idempotency_key,
        IdempotencyRecord.method == body.method, IdempotencyRecord.route_scope == body.route_scope,
        IdempotencyRecord.expires_at > utcnow(),
    ))
    if not record:
        return ok(request, {"operation_status": "NOT_FOUND"})
    data = record.response_data or {}
    case_id = data.get("case_id") or data.get("receipt", {}).get("case_id")
    if not case_id and body.route_scope.rstrip("/") == "/api/v1/cases":
        case_id = data.get("id")
    if case_id:
        case_for(db, p, case_id)
    elif record.status == "SUCCEEDED":
        # Unknown response contracts must not be replayed by a generic endpoint.
        return ok(request, {"operation_status": record.status})
    return ok(request, {"operation_status": record.status,
                        "receipt": data.get("receipt"), "resource_id": data.get("id") or case_id})


def _task_for(db: Session, p: Principal, task_id: str, *, staff=False, write=False):
    task = db.get(Task, task_id)
    if not task:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "找不到可存取的任務。")
    case = (_staff_case if staff else _applicant_case)(db, p, task.case_id, write=write)
    if write:
        db.refresh(task, with_for_update=True)
    return task, case


def _save_task_revision(db: Session, task: Task, p: Principal, reason: str):
    db.add(TaskRevision(task_id=task.id, revision_no=task.current_revision_no,
                        title=task.title, requirement=task.requirement,
                        acceptance_criteria=task.acceptance_criteria, example_ref=task.example_ref,
                        due_at=task.due_at, change_reason=reason, changed_by=p.account.id))


def _submission_view(db: Session, submission: Submission):
    receipt = db.scalar(select(Receipt).where(Receipt.submission_id == submission.id))
    return {"id": submission.id, "case_id": submission.case_id, "task_id": submission.task_id,
            "task_revision": submission.task_revision, "submitted_at": submission.submitted_at,
            "deadline_snapshot": submission.deadline_snapshot,
            "statement": submission.statement, "file_version_ids": submission.file_version_ids,
            "receipt": receipt.snapshot if receipt else None}


@router.get("/tasks")
def list_tasks(request: Request, db: DB, p: User, case_id: str | None = None,
               status: str | None = None, cursor: str | None = None,
               limit: int = Query(default=20, ge=1, le=100)):
    _require_role(p, "applicant")
    own = select(CaseAccess.case_id).where(
        CaseAccess.account_id == p.account.id, CaseAccess.revoked_at.is_(None),
        or_(CaseAccess.expires_at.is_(None), CaseAccess.expires_at > utcnow()),
    )
    query = select(Task).where(Task.case_id.in_(own))
    if case_id:
        _applicant_case(db, p, case_id)
        query = query.where(Task.case_id == case_id)
    if status:
        if status not in TASK_STATES:
            raise ApiError(422, "STATUS_INVALID", "任務狀態不正確。")
        query = query.where(Task.status == status)
    rows, next_cursor = _page(db, query, Task, cursor, limit)
    return ok(request, {"items": [_task_view(t, db=db, p=p) for t in rows], "next_cursor": next_cursor})


@router.get("/tasks/{task_id}")
def get_task(task_id: str, request: Request, db: DB, p: User):
    task, _ = _task_for(db, p, task_id)
    return ok(request, _task_view(task, db=db, p=p), headers={"ETag": etag(task)})


@router.get("/staff/tasks/{task_id}")
def get_staff_task(task_id: str, request: Request, db: DB, p: User):
    task, _ = _task_for(db, p, task_id, staff=True)
    data = _task_view(task, staff=bool(p.roles.intersection({"reviewer", "supervisor"})), db=db, p=p)
    if not p.roles.intersection({"reviewer", "supervisor"}):
        data["allowed_actions"] = []
    return ok(request, data, headers={"ETag": etag(task)})


@router.get("/tasks/{task_id}/submissions")
def task_submissions(task_id: str, request: Request, db: DB, p: User,
                     cursor: str | None = None, limit: int = Query(default=20, ge=1, le=100)):
    _task_for(db, p, task_id)
    rows, next_cursor = _page(db, select(Submission).where(Submission.task_id == task_id),
                             Submission, cursor, limit)
    return ok(request, {"items": [_submission_view(db, s) for s in rows], "next_cursor": next_cursor})


@router.get("/staff/tasks/{task_id}/submissions")
def staff_task_submissions(task_id: str, request: Request, db: DB, p: User,
                           cursor: str | None = None, limit: int = Query(default=20, ge=1, le=100)):
    _task_for(db, p, task_id, staff=True)
    rows, next_cursor = _page(db, select(Submission).where(Submission.task_id == task_id),
                             Submission, cursor, limit)
    return ok(request, {"items": [_submission_view(db, s) for s in rows], "next_cursor": next_cursor})


@router.get("/tasks/{task_id}/revisions")
def task_revisions(task_id: str, request: Request, db: DB, p: User):
    _task_for(db, p, task_id)
    rows = db.scalars(select(TaskRevision).where(TaskRevision.task_id == task_id)
                      .order_by(TaskRevision.revision_no)).all()
    return ok(request, {"items": [{"task_revision": r.revision_no, "title": r.title,
                                   "requirement": r.requirement,
                                   "acceptance_criteria": r.acceptance_criteria,
                                   "due_at": r.due_at, "example_ref": r.example_ref}
                                  for r in rows]})


@router.post("/tasks/{task_id}/submissions")
def submit_task(task_id: str, body: TaskSubmission, request: Request, db: DB, p: User,
                if_match: VersionHeader = None):
    task, case = _task_for(db, p, task_id, write=True)
    record, replay = idem_start(db, p, request, request.url.path, body.model_dump(mode="json"))
    if replay:
        return replay
    check_version(task, if_match)
    if task.status not in {"OPEN", "REOPENED"} or case.status != "UNDER_REVIEW":
        raise ApiError(409, "STATE_CONFLICT", "此任務目前不接受補件。")
    if body.task_revision != task.current_revision_no:
        raise ApiError(412, "VERSION_CONFLICT", "補件要求已更新，請重新讀取。")
    now = _database_time(db)
    scheme = _scheme(db, case.scheme_id)
    if now > task.due_at and not scheme.config.get("allow_late_submission", False):
        raise ApiError(409, "DEADLINE_PASSED", "已超過補件期限，請聯繫承辦。")
    if not body.file_version_ids and not body.statement:
        raise ApiError(422, "SUBMISSION_EMPTY", "請提供補件文件或說明。")
    files = _files(db, case, body.file_version_ids, task_id=task.id)
    receipt = _receipt(db, case, p, body, now, task=task)
    task.status = "SUBMITTED"
    _cancel_task_reminders(db, case.id, task.id)
    _audit(db, p, request, "TASK_SUBMITTED", case, task.id, {"receipt_id": receipt.id})
    _event(db, case, "TASK_SUBMITTED", task=task, payload={"receipt_id": receipt.id})
    data = {"case_id": case.id, "task_id": task.id, "task_status": task.status,
            "version": task.version, "receipt": receipt.snapshot,
            "file_check_status": "CLEAN" if all(f.scan_status == "CLEAN" for f in files) else "PENDING_SCAN",
            "allowed_actions": []}
    headers = {"ETag": etag(task)}
    idem_finish(db, record, data, status_code=201, headers=headers)
    db.commit()
    return ok(request, data, status_code=201, headers=headers)


@router.post("/staff/cases/{case_id}/tasks")
def create_task(case_id: str, body: TaskCreate, request: Request, db: DB, p: User):
    case = _staff_case(db, p, case_id, write=True)
    record, replay = idem_start(db, p, request, request.url.path, body.model_dump(mode="json"))
    if replay:
        return replay
    if case.status != "UNDER_REVIEW":
        raise ApiError(409, "STATE_CONFLICT", "只有審查中的案件可以建立補件任務。")
    if body.due_at <= utcnow():
        raise ApiError(422, "DEADLINE_INVALID", "新任務期限必須在未來。")
    task = Task(id=new_id(), case_id=case.id, assigned_to=case.assigned_to,
                **body.model_dump(), status="OPEN", current_revision_no=1)
    db.add(task)
    db.flush()
    _save_task_revision(db, task, p, "首次建立補件要求")
    _audit(db, p, request, "TASK_CREATED", case, task.id)
    _event(db, case, "TASK_CREATED", task=task)
    data = _task_view(task, staff=True)
    headers = {"ETag": etag(task)}
    idem_finish(db, record, data, status_code=201, headers=headers)
    db.commit()
    return ok(request, data, status_code=201, headers=headers)


@router.post("/staff/tasks/{task_id}/revise")
def revise_task(task_id: str, body: TaskRevise, request: Request, db: DB, p: User,
                if_match: VersionHeader = None):
    task, case = _task_for(db, p, task_id, staff=True, write=True)
    check_version(task, if_match)
    if task.status not in {"OPEN", "REOPENED"} or case.status != "UNDER_REVIEW":
        raise ApiError(409, "STATE_CONFLICT", "此任務目前不能變更要求。")
    changes = body.model_dump(exclude_unset=True, exclude={"reason"})
    if not changes or any(value is None for key, value in changes.items() if key != "example_ref"):
        raise ApiError(422, "REVISION_EMPTY", "請提供有效的修改內容。")
    if body.due_at is not None and body.due_at <= utcnow():
        raise ApiError(422, "DEADLINE_INVALID", "更新後期限必須在未來。")
    for name, value in changes.items():
        setattr(task, name, value)
    task.current_revision_no += 1
    _save_task_revision(db, task, p, body.reason)
    _cancel_task_reminders(db, case.id, task.id)
    _audit(db, p, request, "TASK_REVISED", case, task.id, {"reason": body.reason})
    _event(db, case, "TASK_REVISED", task=task)
    db.commit()
    return ok(request, _task_view(task, staff=True), headers={"ETag": etag(task)})


def _current_submission(db: Session, task: Task, submission_id: str):
    submission = db.get(Submission, submission_id)
    if (not submission or submission.task_id != task.id or
            submission.task_revision != task.current_revision_no):
        raise ApiError(422, "SUBMISSION_NOT_CURRENT", "請指定本任務目前版本的補件。")
    return submission


@router.post("/staff/tasks/{task_id}/accept")
def accept_task(task_id: str, body: TaskAccept, request: Request, db: DB, p: User,
                if_match: VersionHeader = None):
    task, case = _task_for(db, p, task_id, staff=True, write=True)
    check_version(task, if_match)
    if task.status != "SUBMITTED" or case.status != "UNDER_REVIEW":
        raise ApiError(409, "STATE_CONFLICT", "此任務尚無待確認的補件。")
    submission = _current_submission(db, task, body.submission_id)
    _files(db, case, submission.file_version_ids, task_id=task.id, clean=True)
    task.status = "ACCEPTED"
    task.accepted_submission_id = submission.id
    _cancel_task_reminders(db, case.id, task.id)
    _audit(db, p, request, "TASK_ACCEPTED", case, task.id,
           {"submission_id": submission.id, "review_note": body.review_note})
    _event(db, case, "TASK_ACCEPTED", task=task)
    db.commit()
    return ok(request, _task_view(task, staff=True), headers={"ETag": etag(task)})


@router.post("/staff/tasks/{task_id}/reopen")
def reopen_task(task_id: str, body: TaskReopen, request: Request, db: DB, p: User,
                if_match: VersionHeader = None):
    task, case = _task_for(db, p, task_id, staff=True, write=True)
    check_version(task, if_match)
    if task.status != "SUBMITTED" or case.status != "UNDER_REVIEW":
        raise ApiError(409, "STATE_CONFLICT", "只能要求待確認的補件重新提交。")
    _current_submission(db, task, body.submission_id)
    if body.due_at <= utcnow():
        raise ApiError(422, "DEADLINE_INVALID", "重新補件期限必須在未來。")
    task.status = "REOPENED"
    task.due_at = body.due_at
    task.current_revision_no += 1
    task.accepted_submission_id = None
    _save_task_revision(db, task, p, body.public_reason)
    _cancel_task_reminders(db, case.id, task.id)
    _audit(db, p, request, "TASK_REOPENED", case, task.id, {"public_reason": body.public_reason})
    _event(db, case, "TASK_REOPENED", task=task, payload={"public_reason": body.public_reason})
    db.commit()
    return ok(request, _task_view(task, staff=True), headers={"ETag": etag(task)})


@router.post("/staff/tasks/{task_id}/cancel")
def cancel_task(task_id: str, body: Reason, request: Request, db: DB, p: User,
                if_match: VersionHeader = None):
    task, case = _task_for(db, p, task_id, staff=True, write=True)
    check_version(task, if_match)
    if task.status not in {"OPEN", "REOPENED", "SUBMITTED"} or case.status != "UNDER_REVIEW":
        raise ApiError(409, "STATE_CONFLICT", "此任務目前不可取消。")
    task.status = "CANCELLED"
    _cancel_task_reminders(db, case.id, task.id)
    _audit(db, p, request, "TASK_CANCELLED", case, task.id, {"reason": body.reason})
    _event(db, case, "TASK_CANCELLED", task=task)
    db.commit()
    return ok(request, _task_view(task, staff=True), headers={"ETag": etag(task)})


@router.get("/staff/cases")
def staff_cases(request: Request, db: DB, p: User, status: str | None = None,
                assignee: str | None = None, cursor: str | None = None,
                limit: int = Query(default=20, ge=1, le=100)):
    _require_role(p, "reviewer", "supervisor", "auditor")
    query = select(Case).where(case_filter(p))
    if status:
        if status not in CASE_STATES:
            raise ApiError(422, "STATUS_INVALID", "案件狀態不正確。")
        query = query.where(Case.status == status)
    if assignee:
        query = query.where(Case.assigned_to == assignee)
    rows, next_cursor = _page(db, query, Case, cursor, limit)
    return ok(request, {"items": [_case_view(c, p, staff=True, db=db) for c in rows],
                        "next_cursor": next_cursor})


def _review_view(item: ReviewItem):
    return {"id": item.id, "case_id": item.case_id, "criterion_code": item.criterion_code,
            "result": item.result, "internal_note": item.internal_note,
            "public_reason": item.public_reason, "evidence_refs": item.evidence_refs,
            "reviewed_by": item.reviewed_by, "reviewed_at": item.reviewed_at,
            "version": item.version, "etag": etag(item)}


def _decision_view(decision: Decision):
    return {"id": decision.id, "case_id": decision.case_id, "outcome": decision.outcome,
            "reason": decision.reason, "rule_version_id": decision.rule_version_id,
            "evidence_snapshot": decision.evidence_snapshot, "decided_at": decision.decided_at,
            "supersedes_id": decision.supersedes_id}


@router.get("/staff/cases/{case_id}")
def staff_case_detail(case_id: str, request: Request, db: DB, p: User):
    case = _staff_case(db, p, case_id)
    data = _case_view(case, p, staff=True, db=db)
    can_review = bool(p.roles.intersection({"reviewer", "supervisor"}))
    tasks = [_task_view(t, staff=can_review, db=db, p=p)
             for t in db.scalars(select(Task).where(Task.case_id == case_id))]
    if not can_review:
        for task in tasks:
            task["allowed_actions"] = []
    data["tasks"] = tasks
    data["revisions"] = [{"id": r.id, "revision_no": r.revision_no,
                          "schema_version": r.schema_version, "form_snapshot": r.form_snapshot,
                          "submitted_at": r.submitted_at} for r in db.scalars(
                              select(CaseRevision).where(CaseRevision.case_id == case_id)
                              .order_by(CaseRevision.revision_no))]
    data["submissions"] = [_submission_view(db, s) for s in db.scalars(
        select(Submission).where(Submission.case_id == case_id).order_by(Submission.submitted_at))]
    data["files"] = _case_files(db, case_id, submitted_only=True)
    data["decisions"] = [_decision_view(d) for d in db.scalars(
        select(Decision).where(Decision.case_id == case_id).order_by(Decision.decided_at, Decision.id))]
    return ok(request, data, headers={"ETag": etag(case)})


@router.post("/staff/cases/{case_id}/start-review")
def start_review(case_id: str, request: Request, db: DB, p: User,
                 if_match: VersionHeader = None):
    case = _staff_case(db, p, case_id, write=True)
    check_version(case, if_match)
    if case.status != "RECEIVED":
        raise ApiError(409, "STATE_CONFLICT", "只有已收件案件可以開始審查。")
    case.status = "UNDER_REVIEW"
    if case.assigned_to is None:
        case.assigned_to = p.account.id
    scheme = _scheme(db, case.scheme_id)
    criteria = scheme.config.get("required_criteria", [])
    if not criteria or not all(isinstance(c, str) and 0 < len(c) <= 100 for c in criteria):
        raise ApiError(409, "REVIEW_CONFIG_MISSING", "方案尚未核定必要檢核項目。")
    for criterion in dict.fromkeys(criteria):
        db.add(ReviewItem(case_id=case.id, criterion_code=criterion, result="PENDING"))
    _audit(db, p, request, "REVIEW_STARTED", case)
    _event(db, case, "REVIEW_STARTED")
    db.commit()
    return ok(request, _case_view(case, p, staff=True), headers={"ETag": etag(case)})


@router.get("/staff/cases/{case_id}/review-items")
def review_items(case_id: str, request: Request, db: DB, p: User):
    _staff_case(db, p, case_id)
    rows = db.scalars(select(ReviewItem).where(ReviewItem.case_id == case_id)
                      .order_by(ReviewItem.criterion_code)).all()
    return ok(request, {"items": [_review_view(item) for item in rows]})


def _rule(db: Session, case: Case, rule_version_id: str):
    rule = db.get(ContentVersion, rule_version_id)
    if not rule or rule.kind != "RULE" or rule.status != "PUBLISHED" or rule.code != case.scheme_id:
        raise ApiError(422, "RULE_NOT_ALLOWED", "請引用此方案已發布的規則版本。")
    return rule


def _evidence(db: Session, case: Case, references):
    snapshots = []
    submitted_ids = {file_id for row in db.scalars(select(Submission).where(Submission.case_id == case.id))
                     for file_id in row.file_version_ids}
    for ref in references:
        values = ref.model_dump(mode="json")
        rule = _rule(db, case, ref.rule_version_id)
        if not ref.case_revision_id and not ref.file_version_id:
            raise ApiError(422, "EVIDENCE_REQUIRED", "每項依據必須引用正式申請或文件版本。")
        snapshot = {"reference": values, "rule_snapshot": copy.deepcopy(rule.body),
                    "rule_version_no": rule.version_no}
        if ref.case_revision_id:
            revision = db.get(CaseRevision, ref.case_revision_id)
            if not revision or revision.case_id != case.id:
                raise ApiError(422, "EVIDENCE_NOT_ALLOWED", "表單依據不屬於此案件。")
            if ref.field_path:
                value = revision.form_snapshot
                for part in ref.field_path.split("."):
                    if not isinstance(value, dict) or part not in value:
                        raise ApiError(422, "EVIDENCE_FIELD_INVALID", "依據欄位不存在於引用版本。")
                    value = value[part]
            snapshot["form_hash"] = hashlib.sha256(json.dumps(
                revision.form_snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        elif ref.field_path:
            raise ApiError(422, "EVIDENCE_FIELD_INVALID", "欄位引用必須指定表單版本。")
        if ref.file_version_id:
            file_version = db.get(FileVersion, ref.file_version_id)
            file = db.get(File, file_version.file_id) if file_version else None
            if not file or file.case_id != case.id or ref.file_version_id not in submitted_ids:
                raise ApiError(422, "EVIDENCE_NOT_ALLOWED", "文件依據不是此案件的正式提交。")
            _files(db, case, [file_version.id], task_id=file.task_id, clean=True)
            snapshot["file_sha256"] = file_version.sha256
            if ref.replaces_file_version_id:
                previous = db.get(FileVersion, ref.replaces_file_version_id)
                previous_file = db.get(File, previous.file_id) if previous else None
                task = db.get(Task, file.task_id) if file.task_id else None
                accepted = db.get(Submission, task.accepted_submission_id) if task and task.accepted_submission_id else None
                if (not previous_file or previous_file.case_id != case.id or
                        previous.id not in submitted_ids or previous.id == file_version.id or
                        previous_file.document_type != file.document_type or
                        not task or task.status != "ACCEPTED" or not accepted or
                        file_version.id not in accepted.file_version_ids or not ref.note):
                    raise ApiError(422, "REPLACEMENT_NOT_ALLOWED",
                                   "替代文件須同案同類別、已接受且檢查通過，並說明替代理由。")
                snapshot["replaced_file_sha256"] = previous.sha256
        elif ref.page_no:
            raise ApiError(422, "EVIDENCE_PAGE_INVALID", "頁次引用必須指定文件版本。")
        if ref.replaces_file_version_id and not ref.file_version_id:
            raise ApiError(422, "REPLACEMENT_NOT_ALLOWED", "替代關係必須指定目前文件版本。")
        snapshots.append(snapshot)
    return snapshots


@router.patch("/staff/cases/{case_id}/review-items/{item_id}")
def update_review(case_id: str, item_id: str, body: ReviewUpdate, request: Request,
                  db: DB, p: User, if_match: VersionHeader = None):
    case = _staff_case(db, p, case_id, write=True)
    item = db.get(ReviewItem, item_id)
    if not item or item.case_id != case.id:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "找不到檢核項目。")
    check_version(item, if_match)
    if case.status != "UNDER_REVIEW":
        raise ApiError(409, "STATE_CONFLICT", "此案件目前不在審查階段。")
    if body.result in {"PASS", "FAIL"} and not body.evidence_refs:
        raise ApiError(422, "EVIDENCE_REQUIRED", "檢核結論必須有可追溯依據。")
    if body.result in {"FAIL", "QUESTION"} and not (body.public_reason or body.internal_note):
        raise ApiError(422, "REVIEW_REASON_REQUIRED", "請說明不符或疑義原因。")
    _evidence(db, case, body.evidence_refs)
    item.result = body.result
    item.internal_note = body.internal_note
    item.public_reason = body.public_reason
    item.evidence_refs = [ref.model_dump(mode="json") for ref in body.evidence_refs]
    item.reviewed_by = p.account.id
    item.reviewed_at = utcnow()
    _audit(db, p, request, "REVIEW_ITEM_UPDATED", case, item.id, {"result": body.result})
    _event(db, case, "REVIEW_UPDATED", notify=False)
    db.commit()
    return ok(request, _review_view(item), headers={"ETag": etag(item)})


def _ready_for_decision(db: Session, case: Case, body: DecisionCreate, *, correction=False):
    if db.scalar(select(Task.id).where(Task.case_id == case.id,
                                       Task.status.notin_(["ACCEPTED", "CANCELLED"]))):
        raise ApiError(409, "OPEN_TASKS_EXIST", "尚有未完成補件任務。")
    scheme = _scheme(db, case.scheme_id)
    criteria = scheme.config.get("required_criteria", [])
    items = db.scalars(select(ReviewItem).where(ReviewItem.case_id == case.id)).all()
    required = {item.criterion_code: item for item in items if item.criterion_code in criteria}
    if not criteria or set(required) != set(criteria) or any(
            item.result not in {"PASS", "FAIL"} for item in required.values()):
        raise ApiError(409, "REVIEW_INCOMPLETE", "必要檢核尚未完成。")
    if not correction and body.outcome == "APPROVED" and any(item.result != "PASS" for item in required.values()):
        raise ApiError(409, "REVIEW_INCOMPLETE", "仍有不符條件，不能核准。")
    if not correction and body.outcome == "REJECTED" and not any(item.result == "FAIL" for item in required.values()):
        raise ApiError(409, "REVIEW_INCOMPLETE", "駁回須有不符條件的檢核依據。")
    _rule(db, case, body.rule_version_id)
    snapshots = _evidence(db, case, body.evidence_refs)
    replaced = {ref.replaces_file_version_id for ref in body.evidence_refs if ref.replaces_file_version_id}
    replacement_sources = {ref.file_version_id for ref in body.evidence_refs if ref.replaces_file_version_id}
    if replaced.intersection(replacement_sources):
        raise ApiError(422, "REPLACEMENT_CHAIN_INVALID", "請直接引用目前採用的替代版本，不可循環或串接替代。")
    # Pending/failed originals remain blocking unless an explicit, validated
    # accepted replacement is captured in this immutable decision's evidence.
    submissions = db.scalars(select(Submission).where(Submission.case_id == case.id)).all()
    accepted_ids = set(db.scalars(select(Task.accepted_submission_id).where(
        Task.case_id == case.id, Task.status == "ACCEPTED")))
    for submission in submissions:
        if submission.task_id is None or submission.id in accepted_ids:
            active_ids = [file_id for file_id in submission.file_version_ids if file_id not in replaced]
            _files(db, case, active_ids, task_id=submission.task_id, clean=True)
    return snapshots


@router.post("/staff/cases/{case_id}/decisions")
def decide_case(case_id: str, body: DecisionCreate, request: Request, db: DB, p: User,
                if_match: VersionHeader = None):
    case = _staff_case(db, p, case_id, write=True, supervisor=True)
    record, replay = idem_start(db, p, request, request.url.path, body.model_dump(mode="json"))
    if replay:
        return replay
    check_version(case, if_match)
    if case.status != "UNDER_REVIEW":
        raise ApiError(409, "STATE_CONFLICT", "此案件目前不能作成決定。")
    snapshots = _ready_for_decision(db, case, body)
    decision = Decision(id=new_id(), case_id=case.id, outcome=body.outcome, reason=body.reason,
                        rule_version_id=body.rule_version_id, evidence_snapshot=snapshots,
                        decided_by=p.account.id, decided_at=_database_time(db))
    db.add(decision)
    case.status = "DECIDED"
    _audit(db, p, request, "CASE_DECIDED", case, decision.id, {"outcome": body.outcome})
    _event(db, case, "CASE_DECIDED", payload={"outcome": body.outcome})
    data = {"case_id": case.id, "case_status": case.status, "decision": _decision_view(decision)}
    headers = {"ETag": etag(case)}
    idem_finish(db, record, data, status_code=201, headers=headers)
    db.commit()
    return ok(request, data, status_code=201, headers=headers)


@router.post("/staff/cases/{case_id}/decisions/{decision_id}/corrections")
def correct_decision(case_id: str, decision_id: str, body: DecisionCreate, request: Request,
                     db: DB, p: User, if_match: VersionHeader = None):
    case = _staff_case(db, p, case_id, write=True, supervisor=True)
    record, replay = idem_start(db, p, request, request.url.path, body.model_dump(mode="json"))
    if replay:
        return replay
    check_version(case, if_match)
    original = db.get(Decision, decision_id)
    if not original or original.case_id != case.id:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "找不到此案件決定。")
    if case.status not in {"DECIDED", "CLOSED"}:
        raise ApiError(409, "STATE_CONFLICT", "尚未作成決定的案件不可更正。")
    if db.scalar(select(Decision.id).where(Decision.supersedes_id == original.id)):
        raise ApiError(409, "DECISION_SUPERSEDED", "此決定已有更正版本。")
    snapshots = _ready_for_decision(db, case, body, correction=True)
    decision = Decision(id=new_id(), case_id=case.id, outcome=body.outcome, reason=body.reason,
                        rule_version_id=body.rule_version_id, evidence_snapshot=snapshots,
                        decided_by=p.account.id, decided_at=_database_time(db), supersedes_id=original.id)
    db.add(decision)
    case.status = "DECIDED"
    _audit(db, p, request, "CASE_CORRECTED", case, decision.id, {"supersedes_id": original.id})
    _event(db, case, "CASE_CORRECTED", payload={"outcome": body.outcome})
    data = {"case_id": case.id, "case_status": case.status, "decision": _decision_view(decision)}
    headers = {"ETag": etag(case)}
    idem_finish(db, record, data, status_code=201, headers=headers)
    db.commit()
    return ok(request, data, status_code=201, headers=headers)


@router.post("/staff/cases/{case_id}/close")
def close_case(case_id: str, body: CaseClose, request: Request, db: DB, p: User,
               if_match: VersionHeader = None):
    case = _staff_case(db, p, case_id, write=True, supervisor=True)
    check_version(case, if_match)
    if case.status != "DECIDED":
        raise ApiError(409, "STATE_CONFLICT", "尚未核定的案件不可結案。")
    if db.scalar(select(Task.id).where(Task.case_id == case.id,
                                       Task.status.notin_(["ACCEPTED", "CANCELLED"]))):
        raise ApiError(409, "OPEN_TASKS_EXIST", "仍有未完成任務。")
    case.status = "CLOSED"
    _audit(db, p, request, "CASE_CLOSED", case, details={"completion_note": body.completion_note})
    _event(db, case, "CASE_CLOSED")
    db.commit()
    return ok(request, _case_view(case, p, staff=True), headers={"ETag": etag(case)})


@router.post("/staff/case-assignments")
def assign_cases(body: BatchAssignment, request: Request, db: DB, p: User):
    _require_role(p, "supervisor")
    # Lock in stable order. Validate the entire batch before any assignment write.
    cases = []
    for entry in sorted(body.cases, key=lambda item: item.case_id):
        case = _staff_case(db, p, entry.case_id, write=True, supervisor=True)
        if case.version != entry.case_version:
            raise ApiError(412, "VERSION_CONFLICT", "批次包含已更新案件，尚未變更任何分派。")
        if case.status in {"CLOSED", "WITHDRAWN"}:
            raise ApiError(409, "STATE_CONFLICT", "批次包含不可分派案件。")
        cases.append(case)
    assignee = db.get(Account, body.assignee_id)
    if not assignee or assignee.status != "ACTIVE":
        raise ApiError(422, "ASSIGNEE_INVALID", "承辦帳號不存在或已停用。")
    grants = db.scalars(select(RoleGrant).where(RoleGrant.account_id == assignee.id,
                                               RoleGrant.role.in_(["reviewer", "supervisor"]),
                                               RoleGrant.revoked_at.is_(None),
                                               or_(RoleGrant.expires_at.is_(None),
                                                   RoleGrant.expires_at > utcnow()))).all()
    for case in cases:
        if not any(g.scope_type == "GLOBAL" or
                   (g.scope_type == "SCHEME" and g.scope_id == case.scheme_id) or
                   (g.scope_type == "CASE" and g.scope_id == case.id) for g in grants):
            raise ApiError(422, "ASSIGNEE_SCOPE_INVALID", "承辦不具有全部案件的業務範圍。")
    for case in cases:
        case.assigned_to = assignee.id
        _audit(db, p, request, "CASE_ASSIGNED", case,
               details={"assignee_id": assignee.id, "reason": body.reason})
        _event(db, case, "CASE_ASSIGNED", notify=False)
    db.commit()
    return ok(request, {"items": [_case_view(case, p, staff=True) for case in cases]})
