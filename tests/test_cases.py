"""Workflow regression tests, including immutable receipts and object access."""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm.exc import StaleDataError

from app import cases, files
from app.auth_crypto import digest
from app.bootstrap import seed_scheme
from app.common import ApiError
from app.config import Settings
from app.db import Base, make_engine, make_session_factory, utcnow
from app.models import (
    Account, AuditEvent, AuthSession, Case, CaseAccess, CaseRevision, Decision,
    FileVersion, IdempotencyRecord, NotificationIntent, OutboxJob, Receipt, Scheme,
    RoleGrant, Task, TaskRevision,
)
from app.schemas_cases import BatchAssignment, CaseCreate, TaskCreate, TaskSubmission
from app.worker import process_scans


def test_deadlines_require_an_explicit_timezone():
    body = {
        "title": "付款證明",
        "requirement": "請提供付款證明",
        "acceptance_criteria": "付款日期及金額清楚可辨識",
        "due_at": "2030-10-01T12:00:00",
    }
    with pytest.raises(ValidationError):
        TaskCreate.model_validate(body)
    task = TaskCreate.model_validate({**body, "due_at": "2030-10-01T12:00:00+08:00"})
    assert task.due_at.isoformat() == "2030-10-01T04:00:00+00:00"


def test_submission_cannot_duplicate_files_or_exceed_ten():
    with pytest.raises(ValidationError):
        TaskSubmission(task_revision=1, file_version_ids=["same", "same"])
    with pytest.raises(ValidationError):
        TaskSubmission(task_revision=1, file_version_ids=[str(i) for i in range(11)])


def test_request_cannot_set_case_state_or_owner():
    with pytest.raises(ValidationError):
        CaseCreate.model_validate({"scheme_id": "scheme", "status": "APPROVED"})
    with pytest.raises(ValidationError):
        CaseCreate.model_validate({"scheme_id": "scheme", "owner_id": "another-user"})


def test_batch_assignment_rejects_duplicate_cases():
    with pytest.raises(ValidationError):
        BatchAssignment.model_validate({
            "cases": [{"case_id": "same", "case_version": 1}] * 2,
            "assignee_id": "reviewer", "reason": "工作分派",
        })


@pytest.fixture
def workflow(tmp_path):
    settings = Settings(app_env="test", database_url=f"sqlite:///{tmp_path}/workflow.db",
                        secret_key="workflow-test-secret-" * 3,
                        totp_encryption_key=Fernet.generate_key().decode(),
                        storage_dir=tmp_path / "files", allowed_origins=["http://testserver"])
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    roles = {"owner": "applicant", "other": "applicant", "supervisor": "supervisor",
             "reviewer": "reviewer", "admin": "admin"}
    with factory() as db:
        scheme = seed_scheme(db)
        rule_id = scheme.config["rule_version_id"]
        for name, role in roles.items():
            db.add(Account(id=name, email=f"{name}@example.test", email_verified_at=utcnow()))
        db.flush()
        for name, role in roles.items():
            db.add(RoleGrant(account_id=name, role=role, scope_type="GLOBAL"))
            db.add(AuthSession(account_id=name, token_hash=digest(settings.secret_key, "session", name),
                               csrf_hash=digest(settings.secret_key, "csrf", "csrf-" + name),
                               auth_method="email_otp" if role == "applicant" else "staff_mfa",
                               expires_at=utcnow() + timedelta(hours=1)))
        db.commit()
    application = FastAPI()
    application.state.settings = settings
    application.state.session_factory = factory

    @application.exception_handler(ApiError)
    async def api_error(request, exc):
        return JSONResponse({"error": {"code": exc.code, "field_errors": exc.field_errors}},
                            status_code=exc.status)

    @application.exception_handler(StaleDataError)
    async def stale_error(request, exc):
        return JSONResponse({"error": {"code": "VERSION_CONFLICT"}}, status_code=412)

    application.include_router(cases.router, prefix="/api/v1")
    application.include_router(files.router, prefix="/api/v1")
    with TestClient(application) as client:
        def call(actor, method, path, body=None, *, etag=None, key=None, headers=None, content=None):
            request_headers = {"Cookie": f"{settings.session_cookie_name}={actor}",
                               "X-CSRF-Token": "csrf-" + actor, "Origin": "http://testserver"}
            if etag:
                request_headers["If-Match"] = etag
            if key:
                request_headers["Idempotency-Key"] = key
            request_headers.update(headers or {})
            return client.request(method, "/api/v1" + path if not path.startswith("/api/") else path,
                                  json=body, headers=request_headers, content=content)

        yield SimpleNamespace(call=call, factory=factory, settings=settings, rule_id=rule_id)
    engine.dispose()


FORM = {"name": "申請人", "email": "owner@example.test", "subject": "青年申請"}
PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


def new_draft(env, actor="owner"):
    response = env.call(actor, "POST", "/cases", {"scheme_id": "youth-demo"}, key=str(uuid4()))
    assert response.status_code == 201, response.text
    case_id = response.json()["data"]["id"]
    response = env.call(actor, "PATCH", f"/cases/{case_id}", {"form_data": FORM},
                        etag=response.headers["etag"])
    assert response.status_code == 200, response.text
    return case_id, response.headers["etag"]


def received_case(env):
    case_id, tag = new_draft(env)
    response = env.call("owner", "POST", f"/cases/{case_id}/submit", {"file_version_ids": []},
                        key=str(uuid4()), etag=tag)
    assert response.status_code == 201, response.text
    return case_id, response.headers["etag"]


def review_case(env):
    case_id, tag = received_case(env)
    response = env.call("supervisor", "POST", f"/staff/cases/{case_id}/start-review", etag=tag)
    assert response.status_code == 200, response.text
    return case_id


def new_task(env, case_id):
    response = env.call("supervisor", "POST", f"/staff/cases/{case_id}/tasks", {
        "title": "付款證明", "requirement": "請補付款證明",
        "acceptance_criteria": "付款日期及金額完整", "due_at": (utcnow() + timedelta(days=2)).isoformat(),
    }, key=str(uuid4()))
    assert response.status_code == 201, response.text
    return response.json()["data"]


def upload(env, case_id, task_id=None):
    body = {"case_id": case_id, "file_name": "proof.pdf", "content_type": "application/pdf",
            "size_bytes": len(PDF_BYTES), "document_type": "PAYMENT_PROOF"}
    if task_id:
        body["task_id"] = task_id
    response = env.call("owner", "POST", "/files/upload-intents", body)
    assert response.status_code == 201, response.text
    intent = response.json()["data"]
    response = env.call("owner", "PUT", intent["upload_url"], headers=intent["upload_headers"], content=PDF_BYTES)
    assert response.status_code == 200, response.text
    response = env.call("owner", "POST", f"/files/{intent['file_id']}/complete",
                        {"file_version_id": intent["file_version_id"]})
    assert response.status_code == 200, response.text
    return intent


def test_end_to_end_submit_supplement_accept_decide_close(workflow):
    env = workflow
    case_id = review_case(env)
    task = new_task(env, case_id)
    intent = upload(env, case_id, task["id"])
    payload = {"task_revision": task["task_revision"], "file_version_ids": [intent["file_version_id"]]}
    key = str(uuid4())
    response = env.call("owner", "POST", f"/tasks/{task['id']}/submissions", payload,
                        etag=task["etag"], key=key)
    assert response.status_code == 201, response.text
    submitted = response.json()["data"]
    repeated = env.call("owner", "POST", f"/tasks/{task['id']}/submissions", payload,
                       etag=task["etag"], key=key)
    assert repeated.status_code == 201
    assert repeated.json()["data"]["receipt"] == submitted["receipt"]
    receipt_id = submitted["receipt"]["receipt_id"]
    assert submitted["file_check_status"] == "PENDING_SCAN"
    accept = {"submission_id": submitted["receipt"]["submission_id"], "review_note": "資料已完整"}
    rejected = env.call("supervisor", "POST", f"/staff/tasks/{task['id']}/accept", accept,
                        etag=response.headers["etag"])
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "FILE_NOT_CLEAN"
    process_scans(env.factory, env.settings)
    accepted = env.call("supervisor", "POST", f"/staff/tasks/{task['id']}/accept", accept,
                        etag=response.headers["etag"])
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["data"]["status"] == "ACCEPTED"
    evidence = [{"file_version_id": intent["file_version_id"], "rule_version_id": env.rule_id, "page_no": 1}]
    items = env.call("supervisor", "GET", f"/staff/cases/{case_id}/review-items").json()["data"]["items"]
    checked = env.call("supervisor", "PATCH", f"/staff/cases/{case_id}/review-items/{items[0]['id']}",
                       {"result": "PASS", "internal_note": "內部測試備註，不可洩漏", "evidence_refs": evidence},
                       etag=items[0]["etag"])
    assert checked.status_code == 200, checked.text
    current = env.call("supervisor", "GET", f"/staff/cases/{case_id}")
    decision = env.call("supervisor", "POST", f"/staff/cases/{case_id}/decisions",
                        {"outcome": "APPROVED", "reason": "檢核通過", "rule_version_id": env.rule_id,
                         "evidence_refs": evidence}, etag=current.headers["etag"], key=str(uuid4()))
    assert decision.status_code == 201, decision.text
    closed = env.call("supervisor", "POST", f"/staff/cases/{case_id}/close",
                      {"completion_note": "已完成後續程序"}, etag=decision.headers["etag"])
    assert closed.status_code == 200, closed.text
    assert closed.json()["data"]["status"] == "CLOSED"
    public = env.call("owner", "GET", f"/cases/{case_id}")
    assert "內部測試備註" not in public.text
    receipt = env.call("owner", "GET", f"/receipts/{receipt_id}")
    assert receipt.json()["data"] == submitted["receipt"]
    with env.factory() as db:
        assert db.scalar(select(func.count()).select_from(CaseRevision)) == 1
        assert db.scalar(select(func.count()).select_from(Receipt)) == 2
        assert db.scalar(select(func.count()).select_from(Decision)) == 1
        assert db.scalar(select(func.count()).select_from(OutboxJob)) > 0
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "CASE_CLOSED")) == 1


def test_receipt_replay_precedes_old_etag_and_lookup_is_authorized(workflow):
    env = workflow
    case_id, tag = new_draft(env)
    key, path, payload = str(uuid4()), f"/cases/{case_id}/submit", {"file_version_ids": []}
    response = env.call("owner", "POST", path, payload, etag=tag, key=key)
    assert response.status_code == 201, response.text
    repeated = env.call("owner", "POST", path, payload, etag=tag, key=key)
    assert repeated.status_code == 201
    assert repeated.json()["data"] == response.json()["data"]
    lookup_body = {"idempotency_key": key, "method": "POST", "route_scope": "/api/v1" + path}
    lookup = env.call("owner", "POST", "/operations/lookup", lookup_body)
    assert lookup.json()["data"]["receipt"] == response.json()["data"]["receipt"]
    unknown = env.call("other", "POST", "/operations/lookup", lookup_body)
    assert unknown.json()["data"]["operation_status"] == "NOT_FOUND"
    mismatched = env.call("owner", "POST", path, {"file_version_ids": ["different"]}, etag=tag, key=key)
    assert mismatched.status_code == 409
    assert mismatched.json()["error"]["code"] == "IDEMPOTENCY_MISMATCH"
    with env.factory() as db:
        grant = db.scalar(select(CaseAccess).where(CaseAccess.case_id == case_id))
        grant.revoked_at = utcnow()
        db.commit()
    assert env.call("owner", "POST", path, payload, etag=tag, key=key).status_code == 404
    assert env.call("owner", "POST", "/operations/lookup", lookup_body).status_code == 404


def test_object_authorization_csrf_and_draft_version_conflicts(workflow):
    env = workflow
    case_id, tag = new_draft(env)
    assert env.call("other", "GET", f"/cases/{case_id}").status_code == 404
    assert env.call("admin", "GET", f"/staff/cases/{case_id}").status_code == 403
    assert env.call("reviewer", "GET", f"/staff/cases/{case_id}").status_code == 404
    payload = {"form_data": FORM}
    assert env.call("owner", "PATCH", f"/cases/{case_id}", payload).status_code == 428
    assert env.call("owner", "PATCH", f"/cases/{case_id}", payload, etag='"stale"').status_code == 412
    assert env.call("owner", "PATCH", f"/cases/{case_id}", payload, etag=tag,
                    headers={"X-CSRF-Token": "bad"}).status_code == 403
    assert env.call("owner", "PATCH", f"/cases/{case_id}", {"form_data": {**FORM, "approved": True}},
                    etag=tag).status_code == 422
    with env.factory() as db:
        assert db.scalar(select(func.count()).select_from(Receipt)) == 0


def test_failed_notification_rolls_back_submission_and_receipt(workflow, monkeypatch):
    env = workflow
    case_id, tag = new_draft(env)
    def fail(*args, **kwargs):
        raise ApiError(503, "OUTBOX_FAILURE", "測試模擬")
    monkeypatch.setattr(cases, "enqueue_notification", fail)
    response = env.call("owner", "POST", f"/cases/{case_id}/submit", {"file_version_ids": []},
                        etag=tag, key=str(uuid4()))
    assert response.status_code == 503
    with env.factory() as db:
        assert db.get(Case, case_id).status == "DRAFT"
        assert db.scalar(select(func.count()).select_from(Receipt)) == 0
        assert db.scalar(select(func.count()).select_from(CaseRevision)) == 0
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "CASE_SUBMITTED")) == 0
        assert db.scalar(select(func.count()).select_from(IdempotencyRecord).where(
            IdempotencyRecord.route_scope == f"/api/v1/cases/{case_id}/submit")) == 0


def test_task_revision_and_reopen_preserve_prior_receipt(workflow):
    env = workflow
    case_id = review_case(env)
    task = new_task(env, case_id)
    original_due = task["due_at"]
    revised = env.call("supervisor", "POST", f"/staff/tasks/{task['id']}/revise",
                       {"due_at": (utcnow() + timedelta(days=4)).isoformat(), "reason": "展延"},
                       etag=task["etag"])
    assert revised.status_code == 200, revised.text
    task = revised.json()["data"]
    submitted = env.call("owner", "POST", f"/tasks/{task['id']}/submissions",
                         {"task_revision": 2, "statement": "提供補充說明", "file_version_ids": []},
                         etag=task["etag"], key=str(uuid4()))
    assert submitted.status_code == 201, submitted.text
    receipt = submitted.json()["data"]["receipt"]
    reopened = env.call("supervisor", "POST", f"/staff/tasks/{task['id']}/reopen",
                        {"submission_id": receipt["submission_id"], "public_reason": "請再補充日期",
                         "due_at": (utcnow() + timedelta(days=5)).isoformat()}, etag=submitted.headers["etag"])
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["data"]["task_revision"] == 3
    assert env.call("owner", "GET", f"/receipts/{receipt['receipt_id']}").json()["data"] == receipt
    with env.factory() as db:
        revisions = db.scalars(select(TaskRevision).where(TaskRevision.task_id == task["id"])
                              .order_by(TaskRevision.revision_no)).all()
        assert len(revisions) == 3
        assert revisions[0].due_at.isoformat() == original_due.replace("Z", "+00:00")
        assert db.get(Case, case_id).status == "UNDER_REVIEW"
        old = db.scalar(select(NotificationIntent).where(NotificationIntent.purpose == "TASK_CREATED"))
        assert old.status == "CANCELLED"


def test_batch_assignment_conflict_is_all_or_nothing(workflow):
    env = workflow
    first, _ = received_case(env)
    second, _ = received_case(env)
    one = env.call("supervisor", "GET", f"/staff/cases/{first}").json()["data"]
    two = env.call("supervisor", "GET", f"/staff/cases/{second}").json()["data"]
    body = {"cases": [{"case_id": first, "case_version": one["version"]},
                       {"case_id": second, "case_version": two["version"] + 1}],
            "assignee_id": "reviewer", "reason": "批次分派"}
    assert env.call("supervisor", "POST", "/staff/case-assignments", body).status_code == 412
    with env.factory() as db:
        assert db.get(Case, first).assigned_to is None
        assert db.get(Case, second).assigned_to is None
    body["cases"][1]["case_version"] = two["version"]
    assert env.call("supervisor", "POST", "/staff/case-assignments", body).status_code == 200
    assert env.call("reviewer", "GET", f"/staff/cases/{first}").status_code == 200


def test_file_from_another_case_is_rejected_and_deadline_enforced(workflow):
    env = workflow
    first, _ = new_draft(env)
    file = upload(env, first)
    second, tag = new_draft(env)
    response = env.call("owner", "POST", f"/cases/{second}/submit",
                        {"file_version_ids": [file["file_version_id"]]}, etag=tag, key=str(uuid4()))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "FILE_NOT_ALLOWED"
    case_id = review_case(env)
    task = new_task(env, case_id)
    with env.factory() as db:
        row = db.get(Task, task["id"])
        row.due_at = utcnow() - timedelta(days=1)
        db.commit()
    task = env.call("owner", "GET", f"/tasks/{task['id']}").json()["data"]
    assert "submit_task" not in task["allowed_actions"]
    response = env.call("owner", "POST", f"/tasks/{task['id']}/submissions",
                        {"task_revision": 1, "statement": "晚到補件"}, etag=task["etag"], key=str(uuid4()))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DEADLINE_PASSED"
    with env.factory() as db:
        assert db.get(Case, case_id).status == "UNDER_REVIEW"


def test_allowed_actions_follow_read_only_access_and_withdrawal_policy(workflow):
    env = workflow
    case_id, _ = new_draft(env)
    with env.factory() as db:
        db.add(CaseAccess(case_id=case_id, account_id="other", permission="READ"))
        scheme = db.get(Scheme, "youth-demo")
        scheme.config = {**scheme.config, "allow_withdrawal": False}
        db.commit()
    readonly = env.call("other", "GET", f"/cases/{case_id}")
    assert readonly.status_code == 200
    assert readonly.json()["data"]["allowed_actions"] == []
    owner = env.call("owner", "GET", f"/cases/{case_id}")
    assert "withdraw_case" not in owner.json()["data"]["allowed_actions"]
    withdrawn = env.call("owner", "POST", f"/cases/{case_id}/withdraw", {"reason": "申請撤回"},
                        etag=owner.headers["etag"])
    assert withdrawn.status_code == 409


def test_explicit_accepted_replacement_unblocks_rejected_original(workflow):
    env = workflow
    case_id, _ = new_draft(env)
    original = upload(env, case_id)
    current = env.call("owner", "GET", f"/cases/{case_id}")
    received = env.call("owner", "POST", f"/cases/{case_id}/submit",
                        {"file_version_ids": [original["file_version_id"]]},
                        etag=current.headers["etag"], key=str(uuid4()))
    assert received.status_code == 201
    original_receipt = received.json()["data"]["receipt"]
    with env.factory() as db:
        db.get(FileVersion, original["file_version_id"]).scan_status = "REJECTED"
        db.commit()
    reviewed = env.call("supervisor", "POST", f"/staff/cases/{case_id}/start-review",
                        etag=received.headers["etag"])
    assert reviewed.status_code == 200
    task = new_task(env, case_id)
    replacement = upload(env, case_id, task["id"])
    process_scans(env.factory, env.settings)
    submitted = env.call("owner", "POST", f"/tasks/{task['id']}/submissions",
                         {"task_revision": 1, "file_version_ids": [replacement["file_version_id"]]},
                         etag=task["etag"], key=str(uuid4()))
    assert submitted.status_code == 201
    accepted = env.call("supervisor", "POST", f"/staff/tasks/{task['id']}/accept",
                        {"submission_id": submitted.json()["data"]["receipt"]["submission_id"],
                         "review_note": "新文件已完整"}, etag=submitted.headers["etag"])
    assert accepted.status_code == 200
    evidence = {"file_version_id": replacement["file_version_id"], "rule_version_id": env.rule_id,
                "replaces_file_version_id": original["file_version_id"], "note": "以確認安全的補件替代原文件"}
    item = env.call("supervisor", "GET", f"/staff/cases/{case_id}/review-items").json()["data"]["items"][0]
    checked = env.call("supervisor", "PATCH", f"/staff/cases/{case_id}/review-items/{item['id']}",
                       {"result": "PASS", "evidence_refs": [evidence]}, etag=item["etag"])
    assert checked.status_code == 200, checked.text
    current = env.call("supervisor", "GET", f"/staff/cases/{case_id}")
    decision_body = {"outcome": "APPROVED", "reason": "已以合格補件完成檢核",
                     "rule_version_id": env.rule_id, "evidence_refs": [evidence]}
    without_replacement = {**decision_body, "evidence_refs": [
        {"file_version_id": replacement["file_version_id"], "rule_version_id": env.rule_id}]}
    blocked = env.call("supervisor", "POST", f"/staff/cases/{case_id}/decisions", without_replacement,
                       etag=current.headers["etag"], key=str(uuid4()))
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "FILE_NOT_CLEAN"
    decision = env.call("supervisor", "POST", f"/staff/cases/{case_id}/decisions", decision_body,
                        etag=current.headers["etag"], key=str(uuid4()))
    assert decision.status_code == 201, decision.text
    snapshot = decision.json()["data"]["decision"]["evidence_snapshot"][0]
    assert snapshot["reference"]["replaces_file_version_id"] == original["file_version_id"]
    assert snapshot["replaced_file_sha256"]
    assert env.call("owner", "GET", f"/receipts/{original_receipt['receipt_id']}").json()["data"] == original_receipt


def test_correction_preserves_original_and_withdrawal_cancels_open_tasks(workflow):
    env = workflow
    case_id = review_case(env)
    detail = env.call("supervisor", "GET", f"/staff/cases/{case_id}").json()["data"]
    evidence = [{"case_revision_id": detail["revisions"][0]["id"],
                 "field_path": "name", "rule_version_id": env.rule_id}]
    item = env.call("supervisor", "GET", f"/staff/cases/{case_id}/review-items").json()["data"]["items"][0]
    checked = env.call("supervisor", "PATCH", f"/staff/cases/{case_id}/review-items/{item['id']}",
                       {"result": "PASS", "evidence_refs": evidence}, etag=item["etag"])
    assert checked.status_code == 200
    current = env.call("supervisor", "GET", f"/staff/cases/{case_id}")
    payload = {"outcome": "APPROVED", "reason": "檢核完成", "rule_version_id": env.rule_id,
               "evidence_refs": evidence}
    created = env.call("supervisor", "POST", f"/staff/cases/{case_id}/decisions", payload,
                       etag=current.headers["etag"], key=str(uuid4()))
    assert created.status_code == 201
    original = created.json()["data"]["decision"]
    corrected = env.call("supervisor", "POST", f"/staff/cases/{case_id}/decisions/{original['id']}/corrections",
                         {**payload, "outcome": "REJECTED", "reason": "主管重新核對原依據後更正認定"},
                         etag=created.headers["etag"], key=str(uuid4()))
    assert corrected.status_code == 201, corrected.text
    public = env.call("owner", "GET", f"/cases/{case_id}").json()["data"]
    assert public["decision"]["outcome"] == "REJECTED"
    with env.factory() as db:
        assert db.get(Decision, original["id"]).outcome == "APPROVED"
        assert db.scalar(select(func.count()).select_from(Decision)) == 2
    other_case = review_case(env)
    task = new_task(env, other_case)
    current = env.call("owner", "GET", f"/cases/{other_case}")
    withdrawn = env.call("owner", "POST", f"/cases/{other_case}/withdraw", {"reason": "不再申請"},
                        etag=current.headers["etag"])
    assert withdrawn.status_code == 200
    with env.factory() as db:
        assert db.get(Task, task["id"]).status == "CANCELLED"
        assert db.scalar(select(NotificationIntent).where(NotificationIntent.task_id == task["id"],
                                                          NotificationIntent.purpose == "TASK_CREATED")).status == "CANCELLED"
