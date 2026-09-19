"""Public, non-persistent evaluation and authorized immutable case snapshots."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access import case_filter, case_for
from app.auth import Principal, current_principal
from app.auth_crypto import digest
from app.auth_limits import consume_limits
from app.bootstrap import GRANT_SCHEME_ID
from app.common import ApiError, audit, check_version, etag, idem_finish, idem_start, ok
from app.db import get_db, new_id, utcnow
from app.models import Case, Decision
from app.precheck_engine import TAIPEI, compare_results, evaluate, load_bundle, public_catalog
from app.precheck_models import PrecheckSnapshot
from app.precheck_schema import PrecheckInput


router = APIRouter(tags=["precheck"])
DB = Annotated[Session, Depends(get_db)]
User = Annotated[Principal, Depends(current_principal)]
VersionHeader = Annotated[str | None, Header(alias="If-Match")]
PRIVATE_HEADERS = {"Cache-Control": "private, no-store"}
SAFETY_TIPS = [
    {"id": "account_security", "title": "帳號與付款安全", "message": "使用獨立密碼及多因素驗證；勿提供驗證碼給代購或陌生人。"},
    {"id": "domain_scope", "title": "網址比對範圍", "message": "網域相符僅表示符合目錄入口，不代表交易或軟體安全。"},
    {"id": "independent_reminder", "title": "資安提醒獨立呈現", "message": "這些提醒不影響行政預檢結果，也不代表已完成防毒掃描或文件查證。"},
]


def _configuration(request: Request):
    settings = request.app.state.settings
    try:
        bundle = load_bundle(settings.precheck_rules_path)
    except (OSError, ValueError, ValidationError):
        # Neither configuration contents nor the local path belong in a public error.
        raise ApiError(503, "PRECHECK_RULES_UNAVAILABLE", "預檢規則無法載入，請稍後重試或洽服務窗口。") from None
    bundle = bundle.model_copy(update={
        "official_application_url": settings.precheck_official_application_url or bundle.official_application_url,
    })
    demo_allowed = settings.app_env != "production" and settings.precheck_demo_enabled
    return bundle, demo_allowed


def _limit_evaluation(request: Request):
    # Never use input fields or forwarding headers as a rate-limit identifier.
    settings = request.app.state.settings
    source = request.client.host if request.client else "unknown"
    source_hash = digest(settings.secret_key, "precheck-source", source)
    with request.app.state.session_factory() as limits_db:
        consume_limits(limits_db, [
            (f"precheck:minute:{source_hash}", 60, 30),
            (f"precheck:hour:{source_hash}", 3600, 300),
        ], utcnow())


def _accessible_case(db, principal, case_id, *, write=False):
    case = case_for(db, principal, case_id, write=write)
    staff_roles = principal.roles.intersection({"reviewer", "supervisor", "auditor"})
    staff_principal = Principal(principal.account, principal.session, principal.grants, staff_roles)
    # Evaluate the staff branch independently: an unrelated staff grant must not
    # upgrade an applicant's case grant through the shared OR-based policy.
    staff_access = bool(staff_roles and db.scalar(select(Case.id).where(
        Case.id == case.id, case_filter(staff_principal, write=write),
    )))
    if not staff_access and case.created_by != principal.account.id:
        raise ApiError(404, "RESOURCE_NOT_FOUND", "找不到可存取的案件。")
    return case, staff_access


def _snapshot_view(row):
    return {
        "id": row.id, "case_id": row.case_id, "sequence": row.sequence,
        "previous_snapshot_id": row.previous_snapshot_id, "input_version": row.input_version,
        "rules_version": row.rules_version, "catalog_version": row.catalog_version,
        "configuration_hash": row.configuration_hash, "executed_at": row.executed_at,
        "inputs": row.inputs, "result": row.result, "differences": row.differences,
        "safety_tips": row.safety_tips,
    }


def _local_application_history(db, principal, case, reference, prior_subsidy, now, *, source_case_ids=None):
    """Summarize only readable same-applicant cases; no decision is treated as a payment."""
    snapshot = reference.get("snapshot") or {}
    public_program = snapshot.get("program_id") == "hsinchu_ai_2026"
    demo_program = reference.get("mode") == "demo"
    expected_scheme = GRANT_SCHEME_ID if public_program else "youth-demo" if demo_program else None
    executed_at = now.astimezone(TAIPEI).isoformat()
    check = {
        "check_id": "local_application_history", "rule_id": "R11" if public_program else "demo:local_application_history",
        "title": "授權本機案件紀錄自查", "execution_status": "pending", "outcome": None,
        "reason_code": "local_history_program_unavailable", "evidence_type": "server_record",
        "related_fields": ["prior_subsidy"], "triggered_by": {"prior_subsidy": prior_subsidy},
        "rule_version": reference.get("rules_version", "unknown"), "executed_at": executed_at,
        "required": True,
        "source": {"title": "本系統目前帳號有權讀取的同申請人、同計畫案件與有效決定紀錄",
                   "url": None, "kind": "local_authorized_record"},
        "record_scope": "same_applicant_same_program_existing_read_permission",
        "cross_agency_checked": False, "payment_status_verified": False,
        "subsidy_received_count": None, "self_report_reconciliation_required": False,
        "status_counts": None,
        "next_step": "核對自述與已授權的案件紀錄；實際受補助、跨機關重複及付款資料仍需有權承辦確認，不自動拒收。",
    }
    if expected_scheme is None or case.scheme_id != expected_scheme:
        check["reason"] = "目前案件方案與預檢規則計畫不一致或未確認，本項沒有可適用的本機歷史範圍，不能宣稱已排除重複補助。"
        check["message"] = check["reason"]
        return check

    # Apply the existing read predicate, even on SAVE. A write to this case must
    # never widen the viewer's ability to inspect the applicant's other cases.
    readable = select(Case.id, Case.status).where(
        Case.created_by == case.created_by, Case.scheme_id == expected_scheme,
        Case.id != case.id, case_filter(principal, write=False),
    )
    records = db.execute(readable).all()
    ids = [record.id for record in records]
    if source_case_ids is not None:
        source_case_ids.extend(ids)
    superseded = select(Decision.supersedes_id).where(Decision.supersedes_id.is_not(None))
    outcomes = {}
    if ids:
        for decision_case, outcome in db.execute(select(Decision.case_id, Decision.outcome).where(
            Decision.case_id.in_(ids), Decision.id.notin_(superseded),
        )):
            outcomes.setdefault(decision_case, []).append(outcome)
    counts = {"draft": 0, "submitted_or_under_review": 0, "withdrawn": 0,
              "not_approved": 0, "approved_payment_unconfirmed": 0, "other_unconfirmed": 0}
    for record in records:
        decisions = outcomes.get(record.id, [])
        if record.status == "DRAFT":
            counts["draft"] += 1
        elif record.status == "WITHDRAWN":
            counts["withdrawn"] += 1
        elif len(decisions) == 1 and decisions[0] == "APPROVED":
            counts["approved_payment_unconfirmed"] += 1
        elif len(decisions) == 1 and decisions[0] == "REJECTED":
            counts["not_approved"] += 1
        elif record.status in {"RECEIVED", "UNDER_REVIEW"} and not decisions:
            # RECEIVED is an application intake state, never a subsidy receipt.
            counts["submitted_or_under_review"] += 1
        else:
            counts["other_unconfirmed"] += 1
    overlapping = (counts["submitted_or_under_review"] + counts["approved_payment_unconfirmed"]
                   + counts["other_unconfirmed"])
    self_report_review = prior_subsidy in {"received", "applied", "unsure"}
    check.update({"execution_status": "completed", "status_counts": counts,
                  "self_report_reconciliation_required": bool(overlapping or self_report_review),
                  "outcome": "manual_review" if overlapping or self_report_review else "no_issue",
                  "reason_code": "local_history_review" if overlapping or self_report_review else "local_history_scope_no_overlap"})
    if overlapping:
        reason = ("在目前授權範圍找到同申請人、同計畫的申請中、核准或其他待確認紀錄，請與自述核對。"
                  "核准不等於已撥款，收件不等於已領補助，不能據此自動拒收。")
    elif self_report_review:
        reason = ("目前可讀取的同計畫紀錄未呈現申請中或核准紀錄，但自述仍涉及已領取、申請中或不確定情形，需核對。"
                  "沒有本機紀錄不能反證未領補助。")
    else:
        reason = ("目前可讀取的同計畫紀錄中，未見申請中或核准待確認紀錄；草稿、撤回與未核准分別計數，不視為已領補助。")
    reason += "此摘要不含其他案件識別資訊；實際撥款及跨機關資料未介接，不能宣稱已排除跨機關重複補助。"
    if demo_program:
        reason = "DEMO 合成計畫紀錄｜" + reason
    check["reason"] = check["message"] = reason
    return check


def _with_local_history(result, check):
    """Append a server-only check and recompute coverage without trusting client summaries."""
    result = copy.deepcopy(result)
    result["checks"] = [item for item in result["checks"] if item["check_id"] != check["check_id"]] + [check]
    required = [item for item in result["checks"] if item["required"]]
    completed = sum(item["execution_status"] == "completed" for item in required)
    incomplete = len(required) - completed
    issues = sum(item["outcome"] in {"action_needed", "manual_review"} for item in required)
    if incomplete:
        message = f"尚有 {incomplete} 項必要檢查未完成，不能判定整體無異常；請補充或洽承辦確認。"
    elif issues:
        message = f"預檢有 {issues} 項需處理或人工確認；不代表正式案件已核定或拒收。"
    else:
        message = "已執行的自填及授權本機預檢未發現異常；文件、實際撥款及跨機關重複仍未核對。"
    prefix = "DEMO 演示結果（合成規則）｜" if result.get("demo") else "公開來源預檢｜"
    result["summary"] = {"required_total": len(required), "completed": completed,
                         "incomplete": incomplete, "issues": issues, "message": prefix + message}
    return result


def _history_source_access(db, principal, case, row, *, comparison=False):
    """Private provenance never leaves configuration_snapshot; missing evidence fails closed."""
    provenance = row.configuration_snapshot.get("local_history_provenance")
    if not isinstance(provenance, dict) or provenance.get("version") != 1:
        return False
    if comparison and provenance.get("comparison_complete") is not True:
        return False
    key = "comparison_source_case_ids" if comparison else "source_case_ids"
    source_ids = provenance.get(key)
    if not isinstance(source_ids, list) or any(not isinstance(value, str) for value in source_ids):
        return False
    source_ids = set(source_ids)
    if not source_ids:
        return True
    visible = set(db.scalars(select(Case.id).where(
        Case.id.in_(source_ids), Case.id != case.id,
        Case.created_by == case.created_by, Case.scheme_id == case.scheme_id,
        case_filter(principal, write=False),
    )))
    return visible == source_ids


def _redacted_history_check(original):
    # Build an allowlist projection, rather than preserving unknown future aggregate fields.
    reason = "此歷史摘要的來源權限或來源證據無法確認，已隱去紀錄數量與原判定；不表示沒有其他案件或已排除重複補助。"
    return {
        "check_id": "local_application_history", "rule_id": original.get("rule_id", "R11"),
        "title": "歷史案件摘要已依權限隱藏", "execution_status": "pending", "outcome": None,
        "reason_code": "local_history_visibility_redacted", "reason": reason, "message": reason,
        "next_step": "請由具有完整來源權限的承辦查看；本次可見範圍另見最新授權紀錄提示。",
        "evidence_type": "server_record", "related_fields": ["prior_subsidy"],
        "triggered_by": {}, "rule_version": original.get("rule_version", "unknown"),
        "executed_at": original.get("executed_at"), "required": True, "redacted": True,
        "source": {"title": "歷史來源可見範圍未確認", "url": None, "kind": "local_authorized_record"},
        "status_counts": None, "subsidy_received_count": None,
        "self_report_reconciliation_required": None, "cross_agency_checked": False,
        "payment_status_verified": False,
    }


def _remove_history_diff_entries(value):
    """Remove the category itself as evidence: a masked item in 'resolved' still leaks an old result."""
    if isinstance(value, list):
        return [_remove_history_diff_entries(item) for item in value
                if not isinstance(item, dict) or item.get("check_id") != "local_application_history"]
    if isinstance(value, dict):
        return {key: _remove_history_diff_entries(item) for key, item in value.items()
                if not isinstance(item, dict) or item.get("check_id") != "local_application_history"}
    return value


def _snapshot_projection(db, principal, case, row):
    """Current authorization changes the response projection, never the immutable stored evidence."""
    projected = copy.deepcopy(_snapshot_view(row))
    check = next((item for item in projected["result"].get("checks", [])
                  if item.get("check_id") == "local_application_history"), None)
    history_access = _history_source_access(db, principal, case, row)
    if check and not history_access:
        projected["result"] = _with_local_history(projected["result"], _redacted_history_check(check))
        projected["history_details_redacted"] = True
    if not history_access or not _history_source_access(db, principal, case, row, comparison=True):
        differences = _remove_history_diff_entries(projected["differences"])
        differences["history_details_redacted"] = True
        differences["history_redaction_message"] = "歷史紀錄差異的來源權限或來源證據無法確認，已隱去相關差異，不作已解除或新增問題判定。"
        projected["differences"] = differences
    return projected


@router.get("/precheck/catalog")
def catalog(request: Request):
    bundle, demo_allowed = _configuration(request)
    data = public_catalog(bundle, demo_allowed=demo_allowed)
    return ok(request, data, headers=PRIVATE_HEADERS)


@router.post("/precheck/evaluate", dependencies=[Depends(_limit_evaluation)])
def evaluate_anonymously(body: PrecheckInput, request: Request):
    from app.line_handoff import record_server_failure, record_server_result
    try:
        bundle, demo_allowed = _configuration(request)
        result = evaluate(body, bundle, now=utcnow(), demo_allowed=demo_allowed)
    except Exception:
        record_server_failure(request)
        raise
    record_server_result(request, result)
    # No case, input, audit event, idempotency record or result is written here.
    data = {**result, "safety_tips": copy.deepcopy(SAFETY_TIPS)}
    return ok(request, data, headers=PRIVATE_HEADERS)


@router.get("/cases/{case_id}/precheck")
def case_precheck(case_id: str, request: Request, db: DB, p: User):
    case, _staff_access = _accessible_case(db, p, case_id)
    rows = db.scalars(select(PrecheckSnapshot).where(PrecheckSnapshot.case_id == case.id)
                      .order_by(PrecheckSnapshot.sequence.desc())).all()
    history = [_snapshot_projection(db, p, case, row) for row in rows]
    if history:
        reference = history[0]["result"]
        prior_subsidy = history[0]["inputs"].get("prior_subsidy", "unsure")
    else:
        # An unavailable configuration must not prevent reading existing case access.
        try:
            bundle, demo_allowed = _configuration(request)
            reference = public_catalog(bundle, demo_allowed=demo_allowed)
        except ApiError:
            reference = {"mode": "unavailable", "rules_version": "unknown"}
        prior_subsidy = "unsure"
    current_history = _local_application_history(db, p, case, reference, prior_subsidy, utcnow())
    data = {"case_id": case.id, "case_version": case.version,
            "latest": history[0] if history else None, "history": history,
            "differences": history[0]["differences"] if history else None,
            "current_local_application_history": current_history}
    return ok(request, data, headers={**PRIVATE_HEADERS, "ETag": etag(case)})


@router.post("/cases/{case_id}/precheck")
def save_precheck(case_id: str, body: PrecheckInput, request: Request, db: DB, p: User,
                  if_match: VersionHeader = None):
    case, staff_access = _accessible_case(db, p, case_id, write=True)
    inputs = body.model_dump(mode="json")
    record, replay = idem_start(db, p, request, request.url.path, inputs)
    if replay:
        # A prior response may contain a broader history scope than the caller can see now.
        replay_data = copy.deepcopy(record.response_data)
        snapshot_id = replay_data.get("snapshot", {}).get("id")
        replay_row = db.get(PrecheckSnapshot, snapshot_id) if snapshot_id else None
        if replay_row is None or replay_row.case_id != case.id:
            raise ApiError(409, "PRECHECK_REPLAY_UNAVAILABLE", "原預檢結果無法重新確認，請讀取案件最新預檢紀錄。")
        replay_data["snapshot"] = _snapshot_projection(db, p, case, replay_row)
        replay_data["differences"] = replay_data["snapshot"]["differences"]
        from app.line_handoff import record_saved_result
        record_saved_result(request, p, case, replay_data["snapshot"])
        return ok(request, replay_data, record.response_status, record.response_headers)
    check_version(case, if_match)
    if not staff_access and case.status != "DRAFT":
        raise ApiError(409, "STATE_CONFLICT", "青年僅能將預檢保存至本人草稿；預檢保存不等於正式送件。")

    bundle, demo_allowed = _configuration(request)
    now = utcnow()
    result = evaluate(body, bundle, now=now, demo_allowed=demo_allowed)
    history_sources = []
    result = _with_local_history(result, _local_application_history(
        db, p, case, result, body.prior_subsidy, now, source_case_ids=history_sources,
    ))
    previous = db.scalar(select(PrecheckSnapshot).where(PrecheckSnapshot.case_id == case.id)
                         .order_by(PrecheckSnapshot.sequence.desc()).limit(1))
    configuration = {"bundle": bundle.model_dump(mode="json"), "demo_allowed": demo_allowed}
    config_hash = hashlib.sha256(json.dumps(configuration, sort_keys=True, ensure_ascii=False,
                                            separators=(",", ":")).encode()).hexdigest()
    # configuration_hash is the policy configuration hash. Private visibility evidence
    # is deliberately excluded so source/grant changes are not misreported as rule changes.
    comparison_sources = set(history_sources)
    comparison_complete = True
    if previous and any(check.get("check_id") == "local_application_history" for check in previous.result.get("checks", [])):
        provenance = previous.configuration_snapshot.get("local_history_provenance", {})
        old_sources = (provenance.get("source_case_ids")
                       if isinstance(provenance, dict) and provenance.get("version") == 1 else None)
        if not isinstance(old_sources, list) or any(not isinstance(value, str) for value in old_sources):
            comparison_complete = False
        else:
            comparison_sources.update(old_sources)
    configuration["local_history_provenance"] = {
        "version": 1, "source_case_ids": sorted(history_sources),
        "comparison_source_case_ids": sorted(comparison_sources), "comparison_complete": comparison_complete,
        "captured_at": now.astimezone(TAIPEI).isoformat(),
    }
    differences = compare_results(previous.result if previous else None, result)
    differences["configuration_changed"] = bool(previous and previous.configuration_hash != config_hash)
    row = PrecheckSnapshot(
        id=new_id(), case_id=case.id, created_by=p.account.id,
        sequence=previous.sequence + 1 if previous else 1,
        previous_snapshot_id=previous.id if previous else None,
        input_version=result["input_version"], rules_version=result["rules_version"],
        catalog_version=result["catalog_version"], executed_at=now,
        inputs=inputs, result=result, differences=differences,
        configuration_snapshot=configuration, configuration_hash=config_hash,
        safety_tips=copy.deepcopy(SAFETY_TIPS),
    )
    db.add(row)
    # The case version serializes simultaneous saves; its formal fields stay intact.
    case.version += 1
    audit(db, request, actor_id=p.account.id, action="PRECHECK_SAVED", resource_id=row.id,
          case_id=case.id, details={"snapshot_id": row.id, "rules_version": row.rules_version,
                                   "catalog_version": row.catalog_version})
    db.flush()
    projection = _snapshot_projection(db, p, case, row)
    data = {"case_id": case.id, "case_version": case.version,
            "snapshot": projection, "differences": projection["differences"]}
    headers = {**PRIVATE_HEADERS, "ETag": etag(case)}
    idem_finish(db, record, data, status_code=201, headers=headers)
    db.commit()
    from app.line_handoff import record_saved_result
    record_saved_result(request, p, case, data["snapshot"])
    return ok(request, data, status_code=201, headers=headers)
