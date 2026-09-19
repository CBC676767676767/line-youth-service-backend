from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from app import precheck_engine as engine
from app.precheck_schema import PrecheckBundle, PrecheckInput


NOW = datetime(2026, 9, 19, 12, tzinfo=engine.TAIPEI)


@pytest.fixture
def bundle():
    return engine.load_bundle()


@pytest.fixture
def valid_data():
    return {
        "purchase_stage": "purchased", "tool_id": "demo-studio", "plan_id": "monthly-credit",
        "billing_type": "monthly", "purchase_channel": "official", "purchase_url": "https://studio.example",
        "purchase_date": "2026-09-01", "subscription_start": "2026-09-01", "subscription_end": "2026-10-01",
        "residency": "hsinchu", "birth_date": "2000-01-01", "application_type": "standard", "payer": "self",
    }


def run(bundle, data, **changes):
    return engine.evaluate(PrecheckInput.model_validate({**data, **changes}), bundle, now=NOW)


def by_id(result, check_id):
    return next(c for c in result["checks"] if c["check_id"] == check_id)


def test_known_tool_plan_all_completed_self_report(bundle, valid_data):
    result = run(bundle, valid_data)
    assert result["summary"]["issues"] == 0
    assert result["summary"]["incomplete"] == 0
    assert result["summary"]["completed"] == result["summary"]["required_total"]
    assert "DEMO" in result["summary"]["message"]
    assert "尚未核對文件" in result["summary"]["message"]
    assert all(c["evidence_type"] == "self_report" for c in result["checks"])
    assert all(not d["verified"] and not d["received"] for d in result["documents"])
    assert result["uncovered_checks"]
    assert result["executed_at"].endswith("+08:00")
    assert "confirmed_by" not in engine.public_catalog(bundle)["rules"]


def test_unknown_tool_and_plan_preserve_manual_review(bundle, valid_data):
    result = run(bundle, valid_data, tool_id=None, tool_name="尚未收錄的合成工具")
    assert by_id(result, "tool")["outcome"] == "manual_review"
    assert by_id(result, "tool")["reason_code"] == "unknown_tool"
    assert not any(c["outcome"] == "action_needed" for c in result["checks"])
    result = run(bundle, valid_data, plan_id=None, plan_name="未收錄方案")
    assert by_id(result, "plan")["outcome"] == "manual_review"


def test_aliases_and_mismatched_names(bundle, valid_data):
    result = run(bundle, valid_data, tool_id=None, tool_name="DEMO STUDIO", plan_id=None,
                 plan_name="monthly credit plan")
    assert by_id(result, "tool")["outcome"] == "no_issue"
    assert by_id(result, "plan")["outcome"] == "no_issue"
    result = run(bundle, valid_data, plan_name="另一个未核對方案")
    assert by_id(result, "plan")["outcome"] == "manual_review"


def test_same_brand_subscription_credit_and_topup_differ(bundle, valid_data):
    subscription = run(bundle, valid_data)
    topup = run(bundle, valid_data, plan_id="topup", billing_type="credits")
    assert by_id(subscription, "billing")["outcome"] == "no_issue"
    assert by_id(topup, "billing")["reason_code"] == "standalone_credits_restricted"
    assert by_id(topup, "billing")["outcome"] == "action_needed"
    assert by_id(topup, "subscription_period")["execution_status"] == "not_applicable"


def test_no_credit_keyword_heuristic_and_billing_conflict(bundle, valid_data):
    unknown = run(bundle, valid_data, plan_id=None, plan_name="Credit Token 年訂閱", billing_type="annual")
    assert by_id(unknown, "billing")["outcome"] == "manual_review"
    conflict = run(bundle, valid_data, billing_type="credits")
    assert by_id(conflict, "billing")["reason_code"] == "billing_conflict"


def test_explicit_tool_and_channel_restrictions(bundle, valid_data):
    result = run(bundle, valid_data, tool_id="demo-restricted", plan_id="basic",
                 purchase_url="https://restricted.example")
    assert by_id(result, "tool")["reason_code"] == "tool_restricted"
    for channel in ["agent", "marketplace"]:
        result = run(bundle, valid_data, purchase_channel=channel, seller="合成賣方")
        assert by_id(result, "channel")["reason_code"] == "channel_restricted"
        assert "不代表案件自動拒收" in by_id(result, "channel")["reason"]


@pytest.mark.parametrize("url,outcome", [
    ("HTTPS://STUDIO.EXAMPLE/checkout", "no_issue"),
    ("https://pay.studio.example/", "no_issue"),
    ("https://studio.example./", "no_issue"),
    ("https://studio.example.attacker.test/", "manual_review"),
    ("https://fakestudio.example/", "manual_review"),
    ("https://studio-example.test/", "manual_review"),
])
def test_hostname_exact_and_subdomain_boundaries(bundle, valid_data, url, outcome):
    result = run(bundle, valid_data, purchase_url=url)
    assert by_id(result, "domain")["outcome"] == outcome


@pytest.mark.parametrize("url", [
    "https://studio.example@attacker.test", "https://user:secret@studio.example", "//studio.example",
    "https://studio.example:bad", "https://studio.example\\@attacker.test", "https://studio.example\n/",
    "file:///studio.example", "javascript:alert(1)", "https://[not-an-ip]/",
])
def test_invalid_or_credential_urls_fail_incomplete(bundle, valid_data, url):
    result = run(bundle, valid_data, purchase_url=url)
    assert by_id(result, "domain")["execution_status"] == "failed"
    assert by_id(result, "domain")["outcome"] is None
    assert result["summary"]["incomplete"] > 0


def test_idna_and_subdomain_opt_in(bundle, valid_data):
    configuration = bundle.model_dump(mode="json")
    configuration["tools"][0]["domains"] = [{"hostname": "測試.example", "include_subdomains": False}]
    unicode_bundle = PrecheckBundle.model_validate(configuration)
    result = run(unicode_bundle, valid_data, purchase_url="https://測試.example")
    assert by_id(result, "domain")["outcome"] == "no_issue"
    ascii_host = "測試.example".encode("idna").decode("ascii")
    result = run(unicode_bundle, valid_data, purchase_url=f"https://{ascii_host}")
    assert by_id(result, "domain")["outcome"] == "no_issue"
    result = run(unicode_bundle, valid_data, purchase_url=f"https://pay.{ascii_host}")
    assert by_id(result, "domain")["outcome"] == "manual_review"


@pytest.mark.parametrize("date_value,outcome", [
    ("2026-01-01", "no_issue"), ("2026-12-31", "no_issue"),
    ("2025-12-31", "action_needed"), ("2027-01-01", "action_needed"),
])
def test_purchase_date_interval_inclusive(bundle, valid_data, date_value, outcome):
    assert by_id(run(bundle, valid_data, purchase_date=date_value), "purchase_date")["outcome"] == outcome


@pytest.mark.parametrize("value,status", [("", "pending"), ("2026-02-30", "failed"), ("2026-2-01", "failed")])
def test_missing_and_invalid_dates_fail_closed(bundle, valid_data, value, status):
    result = run(bundle, valid_data, purchase_date=value, birth_date=value)
    assert by_id(result, "purchase_date")["execution_status"] == status
    assert by_id(result, "birth_date")["outcome"] is None
    assert "未發現異常" not in result["summary"]["message"]


@pytest.mark.parametrize("value,outcome", [
    ("1991-01-01", "no_issue"), ("2008-12-31", "no_issue"),
    ("1990-12-31", "action_needed"), ("2009-01-01", "action_needed"),
])
def test_birth_date_explicit_interval_not_current_age(bundle, valid_data, value, outcome):
    result = run(bundle, valid_data, birth_date=value)
    assert by_id(result, "birth_date")["outcome"] == outcome


def test_calendar_month_end_not_fixed_30_days(bundle, valid_data):
    result = run(bundle, valid_data, subscription_start="2026-01-31", subscription_end="2026-02-28")
    assert by_id(result, "subscription_period")["outcome"] == "no_issue"
    result = run(bundle, valid_data, subscription_start="2026-01-01", subscription_end="2026-01-31")
    assert by_id(result, "subscription_period")["reason_code"] == "subscription_too_short"
    assert engine._add_months(engine._parse_date("2024-01-31"), 1).isoformat() == "2024-02-29"


def test_ambiguous_month_rule_pending(bundle, valid_data):
    configuration = bundle.model_dump(mode="json")
    configuration["rules"]["params"]["subscription_month_semantics"] = None
    result = run(PrecheckBundle.model_validate(configuration), valid_data)
    assert by_id(result, "subscription_period")["reason_code"] == "month_rule_ambiguous"
    assert result["summary"]["incomplete"] > 0


def test_planning_does_not_require_purchase_receipt_or_payer(bundle, valid_data):
    result = run(bundle, valid_data, purchase_stage="planning", purchase_date="", subscription_start="",
                 subscription_end="", payer="unsure")
    for key in ["purchase_date", "subscription_period", "payer", "payer_relationship"]:
        assert by_id(result, key)["execution_status"] == "not_applicable"
        assert by_id(result, key)["required"] is False
    assert "purchase_receipt" not in {d["doc_id"] for d in result["documents"]}
    assert "payment_evidence" not in {d["doc_id"] for d in result["documents"]}
    assert result["summary"]["incomplete"] == 0


def test_personalized_documents_and_self_report_never_verified(bundle, valid_data):
    result = run(bundle, valid_data, payer="other", payer_relationship="合成家長", application_type="student",
                 prepared_documents=["student_status", "purchase_receipt"])
    docs = {d["doc_id"]: d for d in result["documents"]}
    assert docs["student_status"]["status"] == "self_reported_ready"
    assert docs["payer_relationship"]["status"] == "needed"
    assert all(not d["received"] and not d["verified"] for d in docs.values())
    assert by_id(result, "payer_relationship")["outcome"] == "manual_review"
    assert not any(c["outcome"] == "action_needed" for c in result["checks"])


def test_draft_expired_and_demo_disabled_rules_stay_pending(bundle, valid_data):
    config = bundle.model_dump(mode="json")
    config["rules"]["status"] = "draft"
    draft = run(PrecheckBundle.model_validate(config), valid_data)
    expired = engine.evaluate(PrecheckInput(**valid_data), bundle, now=datetime(2027, 1, 1, tzinfo=engine.TAIPEI))
    disabled = engine.evaluate(PrecheckInput(**valid_data), bundle, now=NOW, demo_allowed=False)
    for result in [draft, expired, disabled]:
        assert result["summary"]["completed"] == 0
        assert result["summary"]["incomplete"] == result["summary"]["required_total"]
        assert all(c["outcome"] is None for c in result["checks"])
        assert not result["documents"]
    assert engine.public_catalog(bundle, demo_allowed=False)["available"] is False
    assert engine.public_catalog(bundle, demo_allowed=False)["tools"] == []


def test_catalog_expired_is_not_confirmation(bundle, valid_data):
    configuration = bundle.model_dump(mode="json")
    configuration["tools"][0]["valid_until"] = "2026-09-18"
    result = run(PrecheckBundle.model_validate(configuration), valid_data)
    assert by_id(result, "tool")["reason_code"] == "catalog_out_of_period"
    assert by_id(result, "domain")["execution_status"] == "pending"


def test_rule_execution_failure_cannot_get_clean_summary(bundle, valid_data, monkeypatch):
    def broken(*_args):
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(engine, "_add_months", broken)
    result = run(bundle, valid_data)
    assert by_id(result, "subscription_period")["reason_code"] == "execution_failed"
    assert result["summary"]["incomplete"] == 1
    assert "未發現異常" not in result["summary"]["message"]


def test_diff_recomputes_resolved_new_pending_and_lost_confirmation(bundle, valid_data):
    old = run(bundle, valid_data, residency="other", birth_date="")
    current = run(bundle, valid_data, birth_date="", purchase_date="", plan_id="topup", billing_type="credits")
    difference = engine.compare_results(old, current)
    assert [c["check_id"] for c in difference["resolved_issues"]] == ["residency"]
    assert "billing" in [c["check_id"] for c in difference["new_issues"]]
    assert "birth_date" in [c["check_id"] for c in difference["still_pending"]]
    assert "purchase_date" in [c["check_id"] for c in difference["lost_confirmation"]]
    assert by_id(current, "residency")["outcome"] == "no_issue"


def test_version_changes_traceable_and_pending_is_not_resolved(bundle, valid_data):
    old = run(bundle, valid_data, residency="other")
    configuration = bundle.model_dump(mode="json")
    configuration["rules"]["version"] = "draft-2026.2"
    configuration["rules"]["status"] = "draft"
    configuration["catalog_version"] = "catalog-2026.2"
    current = run(PrecheckBundle.model_validate(configuration), valid_data)
    difference = engine.compare_results(old, current)
    assert difference["rule_changed"] and difference["catalog_changed"]
    assert not difference["resolved_issues"]
    assert by_id(current, "residency")["rule_version"] == "draft-2026.2"


def test_previously_known_plan_becomes_unknown_loses_confirmation(bundle, valid_data):
    old = run(bundle, valid_data)
    current = run(bundle, valid_data, plan_id=None, plan_name="新方案待確認")
    difference = engine.compare_results(old, current)
    assert "plan" in [c["check_id"] for c in difference["lost_confirmation"]]


def test_timezone_conversion_injected_at_boundary(bundle, valid_data):
    # 2026-12-31 UTC evening is already Jan 1 in Taipei: the demo policy is expired.
    result = engine.evaluate(PrecheckInput(**valid_data), bundle,
                             now=datetime(2026, 12, 31, 16, tzinfo=timezone.utc))
    assert result["executed_at"].startswith("2027-01-01T00:00")
    assert by_id(result, "tool")["reason_code"] == "rules_out_of_period"


def test_input_rejects_forged_flags_and_limits_strings(valid_data):
    for extra in [{"passed": True}, {"verified": True}, {"result": {"outcome": "no_issue"}}]:
        with pytest.raises(ValidationError):
            PrecheckInput(**valid_data, **extra)
    with pytest.raises(ValidationError):
        PrecheckInput(tool_name="x" * 201)


def test_configuration_must_validate_dates_and_sources(bundle, tmp_path):
    config = bundle.model_dump(mode="json")
    config["rules"]["status"] = "confirmed"
    with pytest.raises(ValidationError):
        PrecheckBundle.model_validate(config)
    config = bundle.model_dump(mode="json")
    config["rules"]["params"]["purchase_date_until"] = "2025-12-31"
    with pytest.raises(ValidationError):
        PrecheckBundle.model_validate(config)
    path = tmp_path / "invalid.json"
    path.write_text('{"rules": {}}', encoding="utf-8")
    with pytest.raises(ValidationError):
        engine.load_bundle(path)


def test_confirmed_rules_never_treat_demo_catalog_as_confirmed(bundle, valid_data):
    config = bundle.model_dump(mode="json")
    config["rules"].update({"status": "confirmed", "confirmed_by": "合成測試確認角色",
                             "confirmed_at": "2026-09-01", "source": {
                                 "title": "Synthetic unit-test fixture", "kind": "official",
                                 "url": "https://policy.example/test"}})
    result = run(PrecheckBundle.model_validate(config), valid_data)
    assert by_id(result, "tool")["execution_status"] == "pending"
    assert by_id(result, "billing")["execution_status"] == "pending"


@pytest.fixture
def public_bundle():
    return engine.load_bundle(Path(__file__).parents[1] / "app/data/precheck-hsinchu-115.json")


@pytest.fixture
def public_data(valid_data):
    return {**valid_data, "tool_id": "chatgpt", "plan_id": None, "plan_name": "合成未知方案",
            "billing_component": "subscription", "eligible_cost_twd": "4000",
            "payment_method": "credit_card", "prior_subsidy": "none"}


def test_public_planning_amount_optional_without_demanding_bills(public_bundle, public_data):
    result = run(public_bundle, public_data, purchase_stage="planning", eligible_cost_twd=None,
                 purchase_date="", subscription_start="", subscription_end="", payer="unsure")
    amount = by_id(result, "amount")
    assert amount["execution_status"] == "not_applicable"
    assert amount["required"] is False
    assert not result["estimate"]["available"]
    assert not any(d["doc_id"].startswith(("D02", "D03")) for d in result["documents"])
    estimate = run(public_bundle, public_data, purchase_stage="planning")
    assert estimate["estimate"]["available"]
    assert by_id(estimate, "amount")["required"] is False


@pytest.mark.parametrize("second", [
    {"purchase_date": "2026-09-01", "subscription_start": "2026-09-01", "subscription_end": "2026-10-01"},
    {"purchase_date": "2026-09-15", "subscription_start": "2026-09-15", "subscription_end": "2026-10-15"},
])
def test_public_duplicate_or_overlapping_rows_do_not_double_estimate(public_bundle, public_data, second):
    transactions = [
        {"purchase_date": "2026-09-01", "subscription_start": "2026-09-01", "subscription_end": "2026-10-01",
         "eligible_cost_twd": "2000"},
        {**second, "eligible_cost_twd": "2000"},
    ]
    result = run(public_bundle, public_data, transactions=transactions)
    assert not result["estimate"]["available"]
    assert by_id(result, "monthly_accumulation")["outcome"] == "manual_review"
    assert by_id(result, "amount")["reason_code"] in {"duplicate_monthly_transaction", "overlapping_monthly_transactions"}


def test_monthly_card_documents_each_explain_name_and_last_four(public_bundle, public_data):
    result = run(public_bundle, public_data, transactions=[
        {"purchase_date": "2026-08-01", "subscription_start": "2026-08-01", "subscription_end": "2026-09-01",
         "eligible_cost_twd": "2000"},
        {"purchase_date": "2026-09-01", "subscription_start": "2026-09-01", "subscription_end": "2026-10-01",
         "eligible_cost_twd": "2000"},
    ])
    payment_docs = [doc for doc in result["documents"] if doc["doc_id"].startswith("D03_tx_")]
    assert len(payment_docs) == 2
    assert all("末四碼" in doc["reason"] and "姓名照片" in doc["reason"] for doc in payment_docs)
    assert all(not doc["received"] and not doc["reviewed"] for doc in payment_docs)


def test_public_conflicting_credit_billing_withholds_estimate(public_bundle, public_data):
    result = run(public_bundle, public_data, billing_type="credits", billing_component="included_credits")
    assert by_id(result, "billing")["outcome"] == "manual_review"
    assert not result["estimate"]["available"]


def test_public_verified_plan_conflict_does_not_trust_self_report(public_bundle, public_data):
    config = public_bundle.model_dump(mode="json")
    tool = next(t for t in config["tools"] if t["id"] == "chatgpt")
    tool["catalog_status"] = "verified_plan_catalog"
    tool["plans"] = [{"id": "synthetic_topup", "name": "合成額外額度", "aliases": [],
                      "billing_types": ["credits"], "classification": "standalone_credits"}]
    result = run(PrecheckBundle.model_validate(config), public_data, plan_id="synthetic_topup", plan_name="",
                 billing_type="monthly", billing_component="subscription")
    assert by_id(result, "billing")["reason_code"] == "billing_plan_conflict"
    assert not result["estimate"]["available"]


def test_monthly_reorder_diff_does_not_claim_a_different_row_resolved(public_bundle, public_data):
    first = {"purchase_date": "2026-04-01", "subscription_start": "2026-04-01", "subscription_end": "2026-05-01",
             "eligible_cost_twd": "2000"}
    second = {"purchase_date": "2026-05-01", "subscription_start": "2026-05-01", "subscription_end": "2026-06-01",
              "eligible_cost_twd": "2000"}
    old = run(public_bundle, public_data, transactions=[first, second])
    current = run(public_bundle, public_data, transactions=[second], eligible_cost_twd="2000")
    difference = engine.compare_results(old, current)
    assert difference["transaction_set_changed"]
    assert difference["transaction_changes_require_review"]
    assert difference["transaction_change_message"]
    assert not any(check["check_id"].startswith("transaction_") for check in difference["resolved_issues"])
    assert by_id(current, "transaction_1_purchase_date")["outcome"] == "no_issue"


def test_only_draft_is_not_treated_as_subsidy_received(public_bundle, public_data):
    result = run(public_bundle, public_data, prior_subsidy="draft")
    assert by_id(result, "prior_subsidy")["outcome"] == "no_issue"
    assert "跨機關" in by_id(result, "prior_subsidy")["reason"]
