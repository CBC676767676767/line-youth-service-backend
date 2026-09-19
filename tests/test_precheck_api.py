"""Synthetic end-to-end precheck contracts, privacy and case isolation."""

import copy
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, Table, func, inspect, select, update

from app import precheck
from app.auth_crypto import digest
from app.auth_limits import AuthRateLimit
from app.bootstrap import GRANT_DOCUMENTS, GRANT_SCHEME_ID, seed_grant_scheme, seed_scheme
from app.config import Settings
from app.db import make_engine, make_session_factory, utcnow
from app.main import create_app
from app.models import (
    Account, AuditEvent, AuthSession, Case, CaseAccess, CaseRevision, Decision, DomainEvent,
    IdempotencyRecord, NotificationIntent, RoleGrant, Submission, Task,
)
from app.precheck_engine import DEFAULT_BUNDLE_PATH, load_bundle
from app.precheck_models import PrecheckSnapshot


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = Settings(
        app_env="test", database_url=f"sqlite:///{tmp_path}/precheck.db",
        secret_key="synthetic-precheck-test-secret-" * 2,
        totp_encryption_key=Fernet.generate_key().decode(),
        allowed_origins=["http://testserver"], storage_dir=tmp_path / "files",
        precheck_rules_path=DEFAULT_BUNDLE_PATH,
    )
    application = create_app(settings)
    factory = application.state.session_factory
    roles = {"owner": "applicant", "other": "applicant", "supervisor": "supervisor",
             "reviewer": "reviewer", "auditor": "auditor", "admin": "admin"}
    with factory() as db:
        seed_scheme(db)
        for actor in roles:
            db.add(Account(id=actor, email=f"{actor}@example.test", email_verified_at=utcnow()))
        db.flush()
        for actor, role in roles.items():
            db.add(RoleGrant(account_id=actor, role=role, scope_type="GLOBAL"))
            db.add(AuthSession(
                account_id=actor, token_hash=digest(settings.secret_key, "session", actor),
                csrf_hash=digest(settings.secret_key, "csrf", "csrf-" + actor),
                auth_method="email_otp" if role == "applicant" else "staff_mfa",
                expires_at=utcnow() + timedelta(hours=1),
            ))
        db.commit()
    monkeypatch.setattr(precheck, "utcnow", lambda: datetime(2026, 9, 19, tzinfo=timezone.utc))
    with TestClient(application, raise_server_exceptions=False) as client:
        def call(actor, method, path, body=None, *, tag=None, key=None, headers=None):
            values = {}
            if actor:
                values = {"Cookie": f"{settings.session_cookie_name}={actor}",
                          "X-CSRF-Token": "csrf-" + actor, "Origin": "http://testserver"}
            if tag:
                values["If-Match"] = tag
            if key:
                values["Idempotency-Key"] = key
            values.update(headers or {})
            return client.request(method, "/api/v1" + path, json=body, headers=values)

        def draft(scheme_id="youth-demo"):
            response = call("owner", "POST", "/cases", {"scheme_id": scheme_id}, key=str(uuid4()))
            assert response.status_code == 201, response.text
            return response.json()["data"]["id"], response.headers["ETag"]

        yield SimpleNamespace(call=call, draft=draft, client=client, app=application,
                              factory=factory, settings=settings)


@pytest.fixture
def answers():
    bundle = load_bundle()
    tool = next(item for item in bundle.tools if not item.restricted)
    plan = next(item for item in tool.plans if item.classification == "subscription_included_credits")
    return {
        "purchase_stage": "purchased", "tool_id": tool.id, "plan_id": plan.id,
        "billing_type": plan.billing_types[0], "purchase_channel": "official",
        "purchase_url": "https://" + tool.domains[0].hostname + "/checkout",
        "purchase_date": "2026-09-01", "subscription_start": "2026-09-01",
        "subscription_end": "2026-10-01", "residency": "hsinchu", "birth_date": "2000-06-15",
        "application_type": bundle.rules.params.application_types[0].id, "payer": "self",
    }


def save(env, case_id, tag, answers, key=None, actor="owner"):
    return env.call(actor, "POST", f"/cases/{case_id}/precheck", answers,
                    tag=tag, key=key or str(uuid4()))


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_catalog_is_public_and_has_no_internal_confirmation(env):
    response = env.call(None, "GET", "/precheck/catalog")
    assert response.status_code == 200
    assert response.json()["data"]["official_application_url"] is None
    assert "confirmed_by" not in response.text
    assert "no-store" in response.headers["Cache-Control"]


def test_configured_application_link_is_consistent_and_saved_for_trace(env, answers):
    url = "https://application.example/official"
    env.app.state.settings = env.settings.model_copy(update={"precheck_official_application_url": url})
    catalog = env.call(None, "GET", "/precheck/catalog").json()["data"]
    result = env.call(None, "POST", "/precheck/evaluate", answers).json()["data"]
    case_id, tag = env.draft()
    snapshot = save(env, case_id, tag, answers).json()["data"]["snapshot"]
    assert catalog["official_application_url"] == result["official_application_url"] == url
    assert snapshot["result"]["official_application_url"] == url
    with env.factory() as db:
        saved = db.get(PrecheckSnapshot, snapshot["id"])
        assert saved.configuration_snapshot["bundle"]["official_application_url"] == url


def test_anonymous_evaluation_does_not_store_inputs_or_results(env, answers):
    answers["seller"] = "SYNTHETIC_PRIVATE_SELLER_DO_NOT_STORE"
    models = [Case, PrecheckSnapshot, AuditEvent, IdempotencyRecord, NotificationIntent]
    with env.factory() as db:
        before = [count(db, model) for model in models]
    response = env.call(None, "POST", "/precheck/evaluate", answers)
    assert response.status_code == 200, response.text
    assert "no-store" in response.headers["Cache-Control"]
    assert response.json()["data"]["mode"] == "demo"
    with env.factory() as db:
        assert [count(db, model) for model in models] == before
        rows = db.scalars(select(AuthRateLimit)).all()
        assert len(rows) == 2
        assert all(len(row.key_hash) == 64 for row in rows)
        dump = "\n".join(db.connection().connection.driver_connection.iterdump())
        assert answers["seller"] not in dump
        assert answers["birth_date"] not in dump


@pytest.mark.parametrize("field", ["passed", "verified", "result", "name", "bank_account"])
def test_untrusted_result_flags_and_unneeded_personal_data_are_rejected(env, answers, field):
    response = env.call(None, "POST", "/precheck/evaluate", {**answers, field: "SYNTHETIC_SECRET"})
    assert response.status_code == 422, response.text
    assert "SYNTHETIC_SECRET" not in response.text


def test_input_and_body_limits(env, answers):
    response = env.call(None, "POST", "/precheck/evaluate", {**answers, "tool_name": "x" * 201})
    assert response.status_code == 422
    response = env.call(None, "POST", "/precheck/evaluate", {**answers, "seller": "x" * 20_000})
    assert response.status_code == 413
    response = env.client.post("/api/v1/precheck/evaluate", headers={"Content-Type": "application/json"},
                               content=iter([b'{"seller":"', b"x" * 20_000, b'"}']))
    assert response.status_code == 413


def test_rate_limit_uses_server_source_and_survives_forwarded_header_changes(env):
    for index in range(30):
        response = env.call(None, "POST", "/precheck/evaluate", {},
                            headers={"X-Forwarded-For": f"192.0.2.{index}"})
        assert response.status_code == 200, response.text
    response = env.call(None, "POST", "/precheck/evaluate", {}, headers={"X-Forwarded-For": "198.51.100.1"})
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


def test_empty_case_history_uses_case_etag(env):
    case_id, tag = env.draft()
    response = env.call("owner", "GET", f"/cases/{case_id}/precheck")
    assert response.status_code == 200
    assert response.headers["ETag"] == tag
    assert response.json()["data"]["latest"] is None
    assert response.json()["data"]["history"] == []


def test_save_recomputes_immutable_snapshot_without_formal_or_financial_changes(env, answers):
    case_id, tag = env.draft()
    models = [Decision, NotificationIntent, DomainEvent]
    with env.factory() as db:
        before = [count(db, model) for model in models]
        case = db.get(Case, case_id)
        state = (case.status, case.form_data, case.current_revision_no, case.last_business_update_at)
    response = save(env, case_id, tag, answers)
    assert response.status_code == 201, response.text
    snapshot = response.json()["data"]["snapshot"]
    assert snapshot["sequence"] == 1 and snapshot["previous_snapshot_id"] is None
    assert snapshot["inputs"]["birth_date"] == answers["birth_date"]
    assert snapshot["safety_tips"] and "safety_tips" not in snapshot["result"]
    assert response.headers["ETag"] != tag
    with env.factory() as db:
        assert [count(db, model) for model in models] == before
        case = db.get(Case, case_id)
        assert (case.status, case.form_data, case.current_revision_no, case.last_business_update_at) == state
        row = db.get(PrecheckSnapshot, snapshot["id"])
        assert row.configuration_snapshot["bundle"]["rules"]["version"] == row.rules_version
        assert row.created_by == "owner"
        event = db.scalar(select(AuditEvent).where(AuditEvent.action == "PRECHECK_SAVED"))
        assert answers["birth_date"] not in json.dumps(event.details)


def test_save_requires_auth_csrf_and_trusted_origin(env, answers):
    case_id, tag = env.draft()
    path = f"/cases/{case_id}/precheck"
    assert env.call(None, "POST", path, answers, tag=tag, key=str(uuid4())).status_code == 401
    assert env.call("owner", "POST", path, answers, tag=tag, key=str(uuid4()),
                    headers={"X-CSRF-Token": ""}).status_code == 403
    assert env.call("owner", "POST", path, answers, tag=tag, key=str(uuid4()),
                    headers={"Origin": "https://untrusted.example"}).status_code == 403
    assert save(env, case_id, tag, {**answers, "passed": True}).status_code == 422
    with env.factory() as db:
        assert count(db, PrecheckSnapshot) == 0


@pytest.mark.parametrize("policy", ["demo", "public"])
def test_precheck_keeps_existing_submission_decision_and_formal_deadlines_unchanged(env, answers, policy):
    if policy == "public":
        env.app.state.settings = env.settings.model_copy(update={
            "precheck_rules_path": DEFAULT_BUNDLE_PATH.with_name("precheck-hsinchu-115.json"),
        })
    case_id, _ = env.draft()
    deadline = datetime(2026, 10, 8, 9, tzinfo=timezone.utc)
    with env.factory() as db:
        case = db.get(Case, case_id)
        case.status = "DECIDED"
        case.current_revision_no = 1
        case.form_data = {"subject": "Synthetic formally submitted application"}
        db.add(CaseRevision(id="formal-revision", case_id=case_id, revision_no=1,
                            schema_version=1, form_snapshot=copy.deepcopy(case.form_data),
                            submitted_by="owner"))
        db.add(Decision(id="formal-decision", case_id=case_id, outcome="APPROVED",
                        reason="Synthetic existing decision", rule_version_id="formal-rule",
                        evidence_snapshot=[{"synthetic": True}], decided_by="supervisor"))
        db.add(Task(id="formal-task", case_id=case_id, status="ACCEPTED", title="Synthetic correction",
                    requirement="Preserve the existing agency deadline", due_at=deadline))
        db.flush()
        db.add(Submission(id="formal-submission", case_id=case_id, task_id="formal-task", task_revision=1,
                          case_revision_id="formal-revision", submitted_by="owner",
                          statement="Synthetic completed correction", deadline_snapshot=deadline))
        db.commit()

    def formal_records():
        models = [CaseRevision, Decision, Task, Submission]
        with env.factory() as db:
            return {model.__tablename__: [
                {column.key: copy.deepcopy(getattr(row, column.key)) for column in model.__table__.columns}
                for row in db.scalars(select(model).where(model.case_id == case_id)).all()
            ] for model in models}

    before = formal_records()
    assert env.call(None, "POST", "/precheck/evaluate", answers).status_code == 200
    tag = env.call("supervisor", "GET", f"/cases/{case_id}/precheck").headers["ETag"]
    assert save(env, case_id, tag, answers, actor="supervisor").status_code == 201
    assert formal_records() == before
    with env.factory() as db:
        case = db.get(Case, case_id)
        assert case.status == "DECIDED" and case.current_revision_no == 1
        assert case.form_data == {"subject": "Synthetic formally submitted application"}


def test_other_applicants_and_unassigned_staff_cannot_read_or_save(env, answers):
    case_id, tag = env.draft()
    response = save(env, case_id, tag, answers)
    tag = response.headers["ETag"]
    for actor in ["other", "reviewer", "admin", "auditor"]:
        assert env.call(actor, "GET", f"/cases/{case_id}/precheck").status_code == 404
        assert save(env, case_id, tag, answers, actor=actor).status_code == 404
    assert env.call("supervisor", "GET", f"/cases/{case_id}/precheck").status_code == 200


def test_existing_scope_and_read_only_permissions_are_honored(env, answers):
    case_id, tag = env.draft()
    with env.factory() as db:
        case = db.get(Case, case_id)
        case.assigned_to = "reviewer"
        db.add(CaseAccess(case_id=case_id, account_id="auditor", permission="READ", grant_source="TEST"))
        db.commit()
    tag = env.call("reviewer", "GET", f"/cases/{case_id}/precheck").headers["ETag"]
    response = save(env, case_id, tag, answers, actor="reviewer")
    assert response.status_code == 201, response.text
    assert env.call("auditor", "GET", f"/cases/{case_id}/precheck").status_code == 200
    assert save(env, case_id, response.headers["ETag"], answers, actor="auditor").status_code == 404


def test_save_only_own_draft_for_applicant_and_staff_can_update_existing_case(env, answers):
    case_id, _ = env.draft()
    with env.factory() as db:
        case = db.get(Case, case_id)
        case.status = "RECEIVED"
        db.commit()
    tag = env.call("owner", "GET", f"/cases/{case_id}/precheck").headers["ETag"]
    assert save(env, case_id, tag, answers).status_code == 409
    response = save(env, case_id, tag, answers, actor="supervisor")
    assert response.status_code == 201, response.text
    with env.factory() as db:
        assert db.get(Case, case_id).status == "RECEIVED"


@pytest.mark.parametrize("role", ["reviewer", "supervisor", "auditor"])
def test_unrelated_staff_role_does_not_expand_applicant_permissions(env, answers, role):
    case_id, _ = env.draft()
    with env.factory() as db:
        db.add(RoleGrant(account_id="owner", role=role, scope_type="SCHEME", scope_id="other-scheme"))
        db.scalar(select(AuthSession).where(AuthSession.account_id == "owner")).auth_method = "staff_mfa"
        db.get(Case, case_id).status = "RECEIVED"
        db.add(Case(id="foreign-case", created_by="other", scheme_id="youth-demo",
                    case_no="FOREIGN-SYNTHETIC", status="DRAFT"))
        db.flush()
        db.add(CaseAccess(case_id="foreign-case", account_id="owner", permission="WRITE", grant_source="TEST"))
        db.commit()
    tag = env.call("owner", "GET", f"/cases/{case_id}/precheck").headers["ETag"]
    assert save(env, case_id, tag, answers).status_code == 409
    assert env.call("owner", "GET", "/cases/foreign-case/precheck").status_code == 404
    assert save(env, "foreign-case", '"case-foreign-case-v1"', answers).status_code == 404


def test_idempotent_retry_before_stale_etag_and_permission_recheck(env, answers):
    case_id, tag = env.draft()
    key = str(uuid4())
    first = save(env, case_id, tag, answers, key)
    again = save(env, case_id, tag, answers, key)
    assert first.status_code == again.status_code == 201
    assert first.json()["data"] == again.json()["data"]
    assert first.headers["ETag"] == again.headers["ETag"]
    assert save(env, case_id, first.headers["ETag"], {**answers, "payer": "other"}, key).status_code == 409
    with env.factory() as db:
        assert count(db, PrecheckSnapshot) == 1
        db.scalar(select(CaseAccess).where(CaseAccess.case_id == case_id)).revoked_at = utcnow()
        db.commit()
    assert save(env, case_id, tag, answers, key).status_code == 404


def test_missing_and_stale_versions_rollback_idempotency_records(env, answers):
    case_id, tag = env.draft()
    path = f"/cases/{case_id}/precheck"
    assert env.call("owner", "POST", path, answers, tag=tag).status_code == 428
    key = str(uuid4())
    assert save(env, case_id, None, answers, key).status_code == 428
    assert save(env, case_id, '"stale"', answers, key).status_code == 412
    assert save(env, case_id, tag, answers, key).status_code == 201
    assert save(env, case_id, tag, answers).status_code == 412
    with env.factory() as db:
        assert count(db, PrecheckSnapshot) == 1
        assert count(db, IdempotencyRecord) == 2  # Draft creation and one successful save.


def test_failed_rules_load_rolls_back_entire_save(env, answers, monkeypatch):
    case_id, tag = env.draft()
    with env.factory() as db:
        initial_version = db.get(Case, case_id).version
    def broken(_path):
        raise ValueError("CONFIGURATION_SECRET")
    monkeypatch.setattr(precheck, "load_bundle", broken)
    response = save(env, case_id, tag, answers)
    assert response.status_code == 503
    assert "CONFIGURATION_SECRET" not in response.text
    assert "no-store" in response.headers["Cache-Control"]
    with env.factory() as db:
        assert count(db, PrecheckSnapshot) == 0
        assert count(db, IdempotencyRecord) == 1
        assert db.get(Case, case_id).version == initial_version


def test_recomputed_history_preserves_prior_inputs_and_marks_rule_changes(env, answers, monkeypatch):
    case_id, tag = env.draft()
    first = save(env, case_id, tag, {**answers, "tool_id": None, "tool_name": "Unknown Synthetic Tool",
                                  "plan_id": None, "plan_name": "Unknown Plan"})
    assert first.status_code == 201, first.text
    original = copy.deepcopy(first.json()["data"]["snapshot"])
    second = save(env, case_id, first.headers["ETag"], answers)
    assert second.status_code == 201, second.text
    second_snapshot = second.json()["data"]["snapshot"]
    assert second_snapshot["previous_snapshot_id"] == original["id"]
    assert second_snapshot["differences"]["resolved_issues"]
    changed = load_bundle().model_copy(deep=True)
    changed.rules.version += "-revision-2"
    changed.catalog_version += "-revision-2"
    monkeypatch.setattr(precheck, "load_bundle", lambda _path: changed)
    third = save(env, case_id, second.headers["ETag"], answers)
    assert third.status_code == 201, third.text
    diff = third.json()["data"]["differences"]
    assert diff["rule_changed"] and diff["catalog_changed"] and diff["configuration_changed"]
    history = env.call("owner", "GET", f"/cases/{case_id}/precheck").json()["data"]["history"]
    assert [row["sequence"] for row in history] == [3, 2, 1]
    assert history[-1] == original
    with env.factory() as db:
        rows = db.scalars(select(PrecheckSnapshot).order_by(PrecheckSnapshot.sequence)).all()
        assert rows[0].configuration_snapshot["bundle"]["rules"]["version"] != changed.rules.version
        assert rows[-1].configuration_snapshot["bundle"]["rules"]["version"] == changed.rules.version


def test_snapshots_reject_orm_overwrite_and_deletion(env, answers):
    case_id, tag = env.draft()
    saved = save(env, case_id, tag, answers).json()["data"]["snapshot"]
    with env.factory() as db:
        row = db.get(PrecheckSnapshot, saved["id"])
        row.inputs = {"passed": True}
        with pytest.raises(ValueError, match="immutable"):
            db.flush()
        db.rollback()
        db.delete(db.get(PrecheckSnapshot, saved["id"]))
        with pytest.raises(ValueError, match="immutable"):
            db.flush()


def test_demo_rules_cannot_supply_production_determinations(env, answers):
    env.app.state.settings = env.settings.model_copy(update={"app_env": "production"})
    response = env.call(None, "POST", "/precheck/evaluate", answers)
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert all(check["outcome"] is None for check in result["checks"] if check["required"])
    assert any(check["execution_status"] == "pending" for check in result["checks"] if check["required"])


def test_public_advisory_decimal_and_monthly_inputs_persist_with_full_source_snapshot(env):
    path = DEFAULT_BUNDLE_PATH.with_name("precheck-hsinchu-115.json")
    bundle = load_bundle(path)
    env.app.state.settings = env.settings.model_copy(update={"precheck_rules_path": path})
    catalog_response = env.call(None, "GET", "/precheck/catalog")
    assert catalog_response.status_code == 200, catalog_response.text
    catalog = catalog_response.json()["data"]
    assert catalog["mode"] == "public_advisory" and catalog["demo"] is False
    assert catalog["snapshot"]["agency_approved"] is False
    assert catalog["snapshot"]["automatic_approval_enabled"] is False
    assert catalog["official_application_url"] == bundle.official_application_url
    tool = next(tool for tool in catalog["tools"] if tool["catalog_status"] == "listed_example")
    answers = {
        "purchase_stage": "purchased", "tool_id": tool["id"], "plan_name": "合成測試訂閱名稱",
        "billing_type": "monthly", "billing_component": "included_credits", "purchase_channel": "official",
        "purchase_date": "2026-09-01", "subscription_start": "2026-09-01", "subscription_end": "2026-10-01",
        "residency": "hsinchu", "birth_date": "2000-01-01", "application_type": "standard",
        "payer": "self", "payment_method": "credit_card", "prior_subsidy": "none",
        "eligible_cost_twd": "4000.000000", "transactions": [
            {"purchase_date": "2026-08-01", "subscription_start": "2026-08-01",
             "subscription_end": "2026-09-01", "eligible_cost_twd": "1999.123456"},
            {"purchase_date": "2026-09-01", "subscription_start": "2026-09-01",
             "subscription_end": "2026-10-01", "eligible_cost_twd": "2000.876544"},
        ],
    }
    response = env.call(None, "POST", "/precheck/evaluate", answers)
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["mode"] == "public_advisory" and result["demo"] is False
    assert result["input_version"] == "2"
    assert result["rules_version"] == catalog["rules_version"]
    assert result["funding_status"] == "unknown"
    assert result["formal_submission"] is False and result["automatic_approval_enabled"] is False
    assert result["formal_correction_deadline"] is None
    assert isinstance(result["estimate"]["estimated_subsidy_twd"], str)
    assert Decimal(result["estimate"]["estimated_subsidy_twd"]) == Decimal("2000")
    with env.factory() as db:
        assert count(db, PrecheckSnapshot) == count(db, Case) == 0
    case_id, tag = env.draft()
    saved_response = save(env, case_id, tag, answers)
    assert saved_response.status_code == 201, saved_response.text
    snapshot = saved_response.json()["data"]["snapshot"]
    assert snapshot["inputs"]["eligible_cost_twd"] == "4000.000000"
    assert snapshot["inputs"]["transactions"][0]["eligible_cost_twd"] == "1999.123456"
    assert snapshot["inputs"]["transactions"][1]["eligible_cost_twd"] == "2000.876544"
    assert snapshot["input_version"] == "2"
    assert snapshot["result"]["estimate"] == result["estimate"]
    with env.factory() as db:
        row = db.get(PrecheckSnapshot, snapshot["id"])
        source = row.configuration_snapshot["bundle"]["snapshot"]
        assert source["snapshot_version"] == "public_2026_09_19"
        assert source["source_checked_date"] == "2026-09-19"
        assert source["agency_approved"] is False
        assert source["usage"] == "advisory_precheck"
        assert db.get(Case, case_id).status == "DRAFT"


def test_grant_portal_save_does_not_bypass_formal_document_requirements(env):
    with env.factory() as db:
        seed_grant_scheme(db)
        db.commit()
    env.app.state.settings = env.settings.model_copy(update={
        "precheck_rules_path": DEFAULT_BUNDLE_PATH.with_name("precheck-hsinchu-115.json"),
    })
    case_id, tag = env.draft(GRANT_SCHEME_ID)
    formal_form = {
        "name": "合成申請人", "email": "owner@example.test", "birth": "2000-06-15", "city": "新竹市",
        "tool": "ChatGPT", "purchaseDate": "2026-09-01", "amount": "4000", "requested": "2000",
        "channel": "official", "plan": "monthly", "payer": "relative", "special": True,
        "paymentMethod": "card",
    }
    patched = env.call("owner", "PATCH", f"/cases/{case_id}", {"form_data": formal_form}, tag=tag)
    assert patched.status_code == 200, patched.text
    saved = save(env, case_id, patched.headers["ETag"], {
        "purchase_stage": "purchased", "tool_id": "chatgpt", "plan_name": "合成月費方案",
        "billing_type": "monthly", "billing_component": "included_credits", "purchase_channel": "official",
        "purchase_date": "2026-09-01", "subscription_start": "2026-09-01", "subscription_end": "2026-10-01",
        "residency": "hsinchu", "birth_date": "2000-06-15", "application_type": "standard",
        "payer": "other", "payer_relationship": "parents", "payment_method": "credit_card",
        "eligible_cost_twd": "4000", "prepared_documents": ["D01", "D02", "D03", "D04", "D05"],
    })
    assert saved.status_code == 201, saved.text
    assert saved.json()["data"]["snapshot"]["result"]["mode"] == "public_advisory"
    detail = env.call("owner", "GET", f"/cases/{case_id}")
    assert detail.status_code == 200 and detail.json()["data"]["files"] == []
    submitted = env.call("owner", "POST", f"/cases/{case_id}/submit", {"file_version_ids": []},
                         tag=saved.headers["ETag"], key=str(uuid4()))
    assert submitted.status_code == 422, submitted.text
    error = submitted.json()["error"]
    assert error["code"] == "REQUIRED_DOCUMENT_MISSING"
    assert {item["message"].split("：")[-1] for item in error["field_errors"]} == set(GRANT_DOCUMENTS)
    with env.factory() as db:
        case = db.get(Case, case_id)
        assert case.scheme_id == GRANT_SCHEME_ID and case.status == "DRAFT"
        assert case.form_data == formal_form and case.current_revision_no == 0
        assert count(db, PrecheckSnapshot) == 1
        assert count(db, Submission) == count(db, Decision) == count(db, Task) == 0


@pytest.mark.parametrize("actor", ["reviewer", "supervisor"])
def test_grant_portal_precheck_respects_actual_scheme_staff_scope(env, answers, actor):
    with env.factory() as db:
        seed_grant_scheme(db)
        db.commit()
    case_id, _ = env.draft(GRANT_SCHEME_ID)
    with env.factory() as db:
        grant = db.scalar(select(RoleGrant).where(RoleGrant.account_id == actor))
        grant.scope_type, grant.scope_id = "SCHEME", "youth-demo"
        db.get(Case, case_id).assigned_to = "reviewer"
        db.commit()
    tag = env.call("owner", "GET", f"/cases/{case_id}/precheck").headers["ETag"]
    assert env.call(actor, "GET", f"/cases/{case_id}/precheck").status_code == 404
    assert save(env, case_id, tag, answers, actor=actor).status_code == 404
    with env.factory() as db:
        db.scalar(select(RoleGrant).where(RoleGrant.account_id == actor)).scope_id = GRANT_SCHEME_ID
        db.commit()
    assert env.call(actor, "GET", f"/cases/{case_id}/precheck").status_code == 200
    assert save(env, case_id, tag, answers, actor=actor).status_code == 201


def test_alembic_upgrade_preserves_existing_case(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path}/migration.db"
    monkeypatch.setenv("YOUTH_DATABASE_URL", url)
    monkeypatch.setenv("YOUTH_APP_ENV", "test")
    monkeypatch.setenv("YOUTH_SECRET_KEY", "synthetic-migration-secret-" * 2)
    monkeypatch.setenv("YOUTH_TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    command.upgrade(config, "78a60fc3915a")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    with factory() as db:
        seed_scheme(db)
        db.add(Account(id="migration-owner", email="migration@example.test"))
        db.flush()
        # Reflect the table as this revision actually defines it. Inserting the
        # mapped Case would fail whenever a later revision adds a column, which
        # is exactly the upgrade this test exists to exercise.
        legacy = Table("cases", MetaData(), autoload_with=engine)
        values = {"id": "migration-case", "created_by": "migration-owner",
                  "scheme_id": "youth-demo", "case_no": "SYNTHETIC-CASE",
                  "form_data": {"subject": "preserve me"}, "status": "DRAFT",
                  "schema_version": 1, "current_revision_no": 0, "version": 1,
                  "created_at": utcnow(), "updated_at": utcnow(),
                  "last_business_update_at": utcnow()}
        db.execute(legacy.insert().values(
            **{name: value for name, value in values.items() if name in legacy.c}))
        db.commit()
    command.upgrade(config, "head")
    assert "precheck_snapshots" in inspect(engine).get_table_names()
    with factory() as db:
        assert db.get(Case, "migration-case").form_data == {"subject": "preserve me"}
        assert count(db, PrecheckSnapshot) == 0
    engine.dispose()


def history_check(result):
    return next(check for check in result["checks"] if check["check_id"] == "local_application_history")


def add_history_case(env, *, scheme="youth-demo", owner="owner", status="DRAFT", access=True,
                     outcome=None, assigned_to=None):
    case_id = str(uuid4())
    with env.factory() as db:
        db.add(Case(id=case_id, scheme_id=scheme, created_by=owner, assigned_to=assigned_to,
                    case_no="SYNTHETIC-PRIVATE-" + case_id, status=status, form_data={"name": "PRIVATE_SYNTHETIC_NAME"}))
        db.flush()
        if access:
            db.add(CaseAccess(case_id=case_id, account_id="owner", permission="OWNER"))
        if outcome:
            db.add(Decision(case_id=case_id, outcome=outcome, decided_by="supervisor", rule_version_id="synthetic-rule",
                            reason="PRIVATE_SYNTHETIC_DECISION", decided_at=utcnow()))
        db.commit()
    return case_id


def test_local_history_is_server_derived_and_anonymous_remains_unchanged(env, answers):
    prior = add_history_case(env, status="RECEIVED")
    case_id, tag = env.draft()
    anonymous = env.call(None, "POST", "/precheck/evaluate", {**answers, "prior_subsidy": "none"}).json()["data"]
    assert not any(check["check_id"] == "local_application_history" for check in anonymous["checks"])
    saved = save(env, case_id, tag, {**answers, "prior_subsidy": "none"})
    assert saved.status_code == 201, saved.text
    result = saved.json()["data"]["snapshot"]["result"]
    check = history_check(result)
    assert check["evidence_type"] == "server_record" and check["outcome"] == "manual_review"
    assert check["status_counts"]["submitted_or_under_review"] == 1
    assert check["status_counts"]["draft"] == 0  # The current case is excluded.
    assert check["self_report_reconciliation_required"]
    assert check["triggered_by"] == {"prior_subsidy": "none"}
    assert check["payment_status_verified"] is False and check["cross_agency_checked"] is False
    assert check["subsidy_received_count"] is None
    serialized = json.dumps(check, ensure_ascii=False)
    assert prior not in serialized and "PRIVATE_SYNTHETIC" not in serialized
    assert result["summary"]["issues"] >= 1
    required = [item for item in result["checks"] if item["required"]]
    assert result["summary"]["required_total"] == len(required)
    with env.factory() as db:
        assert db.get(Case, case_id).status == "DRAFT"
        assert db.get(Case, prior).status == "RECEIVED"
        assert count(db, Decision) == 0 and count(db, Submission) == 0


@pytest.mark.parametrize("actual_program", [False, True])
def test_local_history_filters_owner_scheme_and_existing_read_permission(env, answers, actual_program):
    with env.factory() as db:
        seed_grant_scheme(db)
        db.commit()
    scheme = GRANT_SCHEME_ID if actual_program else "youth-demo"
    other_scheme = "youth-demo" if actual_program else GRANT_SCHEME_ID
    if actual_program:
        env.app.state.settings = env.settings.model_copy(update={
            "precheck_rules_path": DEFAULT_BUNDLE_PATH.with_name("precheck-hsinchu-115.json"),
        })
    case_id, tag = env.draft(scheme)
    add_history_case(env, scheme=scheme, owner="other", status="RECEIVED", access=True)
    add_history_case(env, scheme=other_scheme, owner="owner", status="RECEIVED", access=True)
    add_history_case(env, scheme=scheme, owner="owner", status="RECEIVED", access=False)
    visible = add_history_case(env, scheme=scheme, owner="owner", status="DRAFT", access=True)
    result = save(env, case_id, tag, {**answers, "prior_subsidy": "none"}).json()["data"]["snapshot"]["result"]
    check = history_check(result)
    assert sum(check["status_counts"].values()) == 1
    assert check["status_counts"]["draft"] == 1 and check["outcome"] == "no_issue"
    assert visible not in json.dumps(check)
    assert check["rule_id"] == ("R11" if actual_program else "demo:local_application_history")
    assert not check["payment_status_verified"]


def test_drafts_withdrawals_and_rejections_are_not_received_subsidies(env, answers):
    case_id, tag = env.draft()
    add_history_case(env, status="DRAFT")
    add_history_case(env, status="WITHDRAWN")
    add_history_case(env, status="DECIDED", outcome="REJECTED")
    result = save(env, case_id, tag, {**answers, "prior_subsidy": "none"}).json()["data"]["snapshot"]["result"]
    check = history_check(result)
    assert check["status_counts"] == {"draft": 1, "withdrawn": 1, "not_approved": 1,
                                      "submitted_or_under_review": 0, "approved_payment_unconfirmed": 0,
                                      "other_unconfirmed": 0}
    assert check["outcome"] == "no_issue" and check["subsidy_received_count"] is None
    assert "不視為已領補助" in check["reason"]


def test_approved_decision_stays_payment_unknown_and_correction_uses_effective_decision(env, answers):
    case_id, tag = env.draft()
    approved = add_history_case(env, status="DECIDED", outcome="APPROVED")
    first = save(env, case_id, tag, {**answers, "prior_subsidy": "none"})
    assert first.status_code == 201, first.text
    check = history_check(first.json()["data"]["snapshot"]["result"])
    assert check["status_counts"]["approved_payment_unconfirmed"] == 1
    assert check["outcome"] == "manual_review" and check["subsidy_received_count"] is None
    assert "核准不等於已撥款" in check["reason"]
    with env.factory() as db:
        original = db.scalar(select(Decision).where(Decision.case_id == approved))
        db.add(Decision(case_id=approved, outcome="REJECTED", decided_by="supervisor", rule_version_id="synthetic-rule",
                        reason="Synthetic correction", supersedes_id=original.id, decided_at=utcnow()))
        db.commit()
    second = save(env, case_id, first.headers["ETag"], {**answers, "prior_subsidy": "rejected"})
    revised = history_check(second.json()["data"]["snapshot"]["result"])
    assert revised["status_counts"]["approved_payment_unconfirmed"] == 0
    assert revised["status_counts"]["not_approved"] == 1
    assert revised["outcome"] == "no_issue"


def test_public_rules_on_demo_case_cannot_claim_applicable_history_clear(env, answers):
    env.app.state.settings = env.settings.model_copy(update={
        "precheck_rules_path": DEFAULT_BUNDLE_PATH.with_name("precheck-hsinchu-115.json"),
    })
    case_id, tag = env.draft()
    response = save(env, case_id, tag, answers)
    result = response.json()["data"]["snapshot"]["result"]
    check = history_check(result)
    assert check["execution_status"] == "pending" and check["outcome"] is None
    assert check["status_counts"] is None
    assert result["summary"]["incomplete"] > 0 and "未發現異常" not in result["summary"]["message"]


def test_get_refreshes_authorized_history_without_rewriting_saved_snapshots(env, answers):
    case_id, tag = env.draft()
    response = save(env, case_id, tag, {**answers, "prior_subsidy": "none"})
    saved = response.json()["data"]["snapshot"]
    assert history_check(saved["result"])["status_counts"]["submitted_or_under_review"] == 0
    add_history_case(env, status="UNDER_REVIEW")
    response = env.call("owner", "GET", f"/cases/{case_id}/precheck")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["latest"] == saved and data["history"][0] == saved
    fresh = data["current_local_application_history"]
    assert fresh["status_counts"]["submitted_or_under_review"] == 1
    assert fresh["outcome"] == "manual_review"
    with env.factory() as db:
        row = db.get(PrecheckSnapshot, saved["id"])
        assert history_check(row.result)["status_counts"]["submitted_or_under_review"] == 0
        assert count(db, PrecheckSnapshot) == 1


def test_staff_history_only_counts_cases_the_viewer_can_read(env, answers):
    case_id, _ = env.draft()
    with env.factory() as db:
        db.get(Case, case_id).assigned_to = "reviewer"
        db.commit()
    add_history_case(env, status="RECEIVED", assigned_to="reviewer")
    add_history_case(env, status="RECEIVED", assigned_to=None)
    response = env.call("reviewer", "GET", f"/cases/{case_id}/precheck")
    assert response.status_code == 200
    check = response.json()["data"]["current_local_application_history"]
    assert check["status_counts"]["submitted_or_under_review"] == 1
    # An unrelated account cannot retrieve either the saved or freshly computed history.
    assert env.call("other", "GET", f"/cases/{case_id}/precheck").status_code == 404


def test_client_cannot_supply_forged_local_history(env, answers):
    case_id, tag = env.draft()
    response = save(env, case_id, tag, {**answers, "local_application_history": {"outcome": "no_issue"}})
    assert response.status_code == 422
    with env.factory() as db:
        assert count(db, PrecheckSnapshot) == 0


def _assign_current_to_reviewer(env, case_id):
    with env.factory() as db:
        db.get(Case, case_id).assigned_to = "reviewer"
        db.commit()
    return env.call("owner", "GET", f"/cases/{case_id}/precheck").headers["ETag"]


def _grant_history_to_reviewer(env, source_id):
    with env.factory() as db:
        grant = CaseAccess(case_id=source_id, account_id="reviewer", permission="READ")
        db.add(grant)
        db.commit()
        return grant.id


def _assert_masked_history_projection(snapshot):
    check = history_check(snapshot["result"])
    assert check["redacted"] is True and check["status_counts"] is None
    assert check["outcome"] is None and check["execution_status"] == "pending"
    assert check["self_report_reconciliation_required"] is None
    assert snapshot["result"]["summary"]["incomplete"] >= 1
    assert "未發現異常" not in snapshot["result"]["summary"]["message"]
    assert snapshot["differences"]["history_details_redacted"]
    for bucket in ["new_issues", "resolved_issues", "still_pending", "lost_confirmation"]:
        assert not any(item.get("check_id") == "local_application_history" for item in snapshot["differences"].get(bucket, []))


def test_historical_aggregate_is_masked_across_roles_and_restored_only_with_all_sources(env, answers):
    source_a = add_history_case(env, status="RECEIVED")
    source_b = add_history_case(env, status="DECIDED", outcome="APPROVED")
    case_id, _ = env.draft()
    tag = _assign_current_to_reviewer(env, case_id)
    saved = save(env, case_id, tag, {**answers, "prior_subsidy": "none"}, actor="supervisor")
    assert saved.status_code == 201, saved.text
    original = saved.json()["data"]["snapshot"]
    assert history_check(original["result"])["status_counts"]["approved_payment_unconfirmed"] == 1
    with env.factory() as db:
        row = db.get(PrecheckSnapshot, original["id"])
        provenance = row.configuration_snapshot["local_history_provenance"]
        assert set(provenance["source_case_ids"]) == {source_a, source_b}
        immutable_result = copy.deepcopy(row.result)
        immutable_diff = copy.deepcopy(row.differences)
    response = env.call("reviewer", "GET", f"/cases/{case_id}/precheck")
    assert response.status_code == 200
    data = response.json()["data"]
    _assert_masked_history_projection(data["latest"])
    assert data["differences"] == data["latest"]["differences"]
    assert sum(data["current_local_application_history"]["status_counts"].values()) == 0
    assert source_a not in response.text and source_b not in response.text
    assert "source_case_ids" not in response.text and "local_history_provenance" not in response.text
    _grant_history_to_reviewer(env, source_a)
    half = env.call("reviewer", "GET", f"/cases/{case_id}/precheck").json()["data"]
    _assert_masked_history_projection(half["latest"])
    grant_b = _grant_history_to_reviewer(env, source_b)
    restored = env.call("reviewer", "GET", f"/cases/{case_id}/precheck").json()["data"]
    assert restored["latest"] == original
    with env.factory() as db:
        db.get(CaseAccess, grant_b).revoked_at = utcnow()
        db.commit()
    revoked = env.call("reviewer", "GET", f"/cases/{case_id}/precheck").json()["data"]
    _assert_masked_history_projection(revoked["latest"])
    with env.factory() as db:
        row = db.get(PrecheckSnapshot, original["id"])
        assert row.result == immutable_result and row.differences == immutable_diff
        assert count(db, PrecheckSnapshot) == 1


def test_legacy_aggregate_without_private_provenance_is_masked_even_for_supervisor(env, answers):
    source = add_history_case(env, status="RECEIVED")
    case_id, tag = env.draft()
    saved = save(env, case_id, tag, {**answers, "prior_subsidy": "none"}, actor="supervisor")
    snapshot = saved.json()["data"]["snapshot"]
    with env.factory() as db:
        row = db.get(PrecheckSnapshot, snapshot["id"])
        legacy_config = copy.deepcopy(row.configuration_snapshot)
        legacy_config.pop("local_history_provenance")
        # Synthetic fixture emulates a row saved by the older release, without changing production migrations.
        db.execute(update(PrecheckSnapshot).where(PrecheckSnapshot.id == row.id).values(configuration_snapshot=legacy_config))
        db.commit()
    response = env.call("supervisor", "GET", f"/cases/{case_id}/precheck")
    data = response.json()["data"]
    _assert_masked_history_projection(data["latest"])
    assert data["current_local_application_history"]["status_counts"]["submitted_or_under_review"] == 1
    assert source not in response.text
    with env.factory() as db:
        row = db.get(PrecheckSnapshot, snapshot["id"])
        assert history_check(row.result)["status_counts"]["submitted_or_under_review"] == 1


def test_narrow_save_masks_comparison_to_broader_snapshot_and_keeps_policy_hash_stable(env, answers):
    source = add_history_case(env, status="RECEIVED")
    case_id, _ = env.draft()
    tag = _assign_current_to_reviewer(env, case_id)
    first = save(env, case_id, tag, {**answers, "prior_subsidy": "none"}, actor="supervisor")
    original = first.json()["data"]["snapshot"]
    second = save(env, case_id, first.headers["ETag"], {**answers, "prior_subsidy": "none"}, actor="reviewer")
    assert second.status_code == 201, second.text
    new_snapshot = second.json()["data"]["snapshot"]
    assert history_check(new_snapshot["result"])["status_counts"]["submitted_or_under_review"] == 0
    assert new_snapshot["differences"]["history_details_redacted"]
    assert not any(check["check_id"] == "local_application_history" for check in new_snapshot["differences"]["resolved_issues"])
    assert new_snapshot["configuration_hash"] == original["configuration_hash"]
    assert new_snapshot["differences"]["configuration_changed"] is False
    assert source not in second.text
    with env.factory() as db:
        row = db.get(PrecheckSnapshot, new_snapshot["id"])
        assert row.configuration_snapshot["local_history_provenance"]["source_case_ids"] == []
        assert row.configuration_snapshot["local_history_provenance"]["comparison_source_case_ids"] == [source]
        assert any(check["check_id"] == "local_application_history" for check in row.differences["resolved_issues"])
        policy_configuration = {key: row.configuration_snapshot[key] for key in ("bundle", "demo_allowed")}
        expected_hash = precheck.hashlib.sha256(json.dumps(policy_configuration, sort_keys=True, ensure_ascii=False,
                                                          separators=(",", ":")).encode()).hexdigest()
        assert row.configuration_hash == expected_hash


def test_idempotent_replay_cannot_restore_revoked_history_counts(env, answers):
    source = add_history_case(env, status="RECEIVED")
    case_id, tag = env.draft()
    key = str(uuid4())
    payload = {**answers, "prior_subsidy": "none"}
    first = save(env, case_id, tag, payload, key=key)
    assert first.status_code == 201
    with env.factory() as db:
        db.scalar(select(CaseAccess).where(CaseAccess.case_id == source, CaseAccess.account_id == "owner")).revoked_at = utcnow()
        db.commit()
    replay = save(env, case_id, tag, payload, key=key)
    assert replay.status_code == 201, replay.text
    data = replay.json()["data"]
    _assert_masked_history_projection(data["snapshot"])
    assert data["differences"] == data["snapshot"]["differences"]
    assert source not in replay.text
    with env.factory() as db:
        assert count(db, PrecheckSnapshot) == 1


@pytest.mark.parametrize("malformed_provenance", [
    None, "legacy-unsupported-format",
    {"version": 1, "comparison_complete": True, "comparison_source_case_ids": []},
])
def test_legacy_malformed_history_provenance_masks_comparison_without_breaking_save(env, answers, malformed_provenance):
    add_history_case(env, status="RECEIVED")
    case_id, tag = env.draft()
    first = save(env, case_id, tag, {**answers, "prior_subsidy": "none"})
    snapshot_id = first.json()["data"]["snapshot"]["id"]
    with env.factory() as db:
        row = db.get(PrecheckSnapshot, snapshot_id)
        configuration = copy.deepcopy(row.configuration_snapshot)
        configuration["local_history_provenance"] = malformed_provenance
        db.execute(update(PrecheckSnapshot).where(PrecheckSnapshot.id == snapshot_id).values(configuration_snapshot=configuration))
        db.commit()
    projected = env.call("owner", "GET", f"/cases/{case_id}/precheck").json()["data"]["latest"]
    _assert_masked_history_projection(projected)
    result = save(env, case_id, first.headers["ETag"], {**answers, "prior_subsidy": "none"})
    assert result.status_code == 201, result.text
    assert result.json()["data"]["differences"]["history_details_redacted"]
    with env.factory() as db:
        assert count(db, PrecheckSnapshot) == 2
