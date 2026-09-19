"""Public policy snapshot tests: all applicant/transaction inputs are synthetic.

This suite loads the actual public JSON; it never invents an approved product plan.
Supplemental requirements 1-24 are mapped in the test names/comments below.
"""

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app import precheck_engine as engine
from app.precheck_schema import PrecheckInput


NOW = datetime(2026, 9, 19, 12, tzinfo=engine.TAIPEI)
POLICY = Path(__file__).parents[1] / "app/data/precheck-hsinchu-115.json"


@pytest.fixture
def public_bundle():
    return engine.load_bundle(POLICY)


@pytest.fixture
def data():
    return {
        "purchase_stage": "purchased", "tool_id": "chatgpt", "plan_name": "合成填答：方案尚待查核",
        "billing_type": "monthly", "billing_component": "subscription", "purchase_channel": "official",
        "purchase_url": "https://synthetic-purchase.example/receipt", "seller": "合成填答賣方",
        "purchase_date": "2026-09-01", "subscription_start": "2026-09-01", "subscription_end": "2026-10-01",
        "residency": "hsinchu", "birth_date": "2000-01-01", "application_type": "standard",
        "payer": "self", "payment_method": "credit_card", "eligible_cost_twd": "4000",
        "prior_subsidy": "none",
    }


def run(bundle, data, *, now=NOW, **changes):
    return engine.evaluate(PrecheckInput.model_validate({**data, **changes}), bundle, now=now)


def check(result, name):
    return next(item for item in result["checks"] if item["check_id"] == name)


def documents(result):
    return {item["doc_id"]: item for item in result["documents"]}


def test_public_snapshot_operates_without_claiming_agency_approval(public_bundle, data):
    snapshot = public_bundle.snapshot.model_dump(mode="json")
    assert snapshot == {
        "program_id": "hsinchu_ai_2026", "snapshot_version": "public_2026_09_19",
        "source_checked_date": "2026-09-19", "timezone": "Asia/Taipei",
        "source_type": "official_public_page", "agency_approved": False,
        "usage": "advisory_precheck", "automatic_approval_enabled": False,
    }
    result = run(public_bundle, data)
    assert result["mode"] == "public_advisory"
    assert check(result, "birth_date")["execution_status"] == "completed"
    assert public_bundle.rules.status == "draft"
    assert public_bundle.rules.confirmed_by is None
    assert public_bundle.input_version == "2"


@pytest.mark.parametrize("birth_date,outcome", [
    ("1985-04-03", "no_issue"), ("2010-04-02", "no_issue"),
    ("1985-04-02", "action_needed"), ("2010-04-03", "action_needed"),
])
def test_01_02_birth_date_boundaries(public_bundle, data, birth_date, outcome):
    assert check(run(public_bundle, data, birth_date=birth_date), "birth_date")["outcome"] == outcome


def test_03_birth_result_does_not_recalculate_current_age(public_bundle, data):
    first = run(public_bundle, data, birth_date="1985-04-03", now=datetime(2026, 8, 14, tzinfo=engine.TAIPEI))
    later = run(public_bundle, data, birth_date="1985-04-03", now=datetime(2026, 11, 30, tzinfo=engine.TAIPEI))
    assert check(first, "birth_date")["outcome"] == check(later, "birth_date")["outcome"] == "no_issue"
    assert check(first, "birth_date")["rule_id"] == "R02"


def test_04_hsinchu_county_is_not_hsinchu_city(public_bundle, data):
    result = run(public_bundle, data, residency="hsinchu_county")
    assert check(result, "residency")["outcome"] == "action_needed"
    assert check(run(public_bundle, data, residency="unsure"), "residency")["outcome"] == "manual_review"
    city = check(run(public_bundle, data), "residency")
    assert city["evidence_type"] == "self_report"


@pytest.mark.parametrize("application_type,cost,expected,cap,categories", [
    ("standard", "4000", "2000", "3000", []),
    ("standard", "10000", "3000", "3000", []),
    ("specific", "4000", "3600", "6000", ["low_income"]),
    ("specific", "10000", "6000", "6000", ["low_income"]),
    ("language", "4000", "3600", "6000", ["language_hakka"]),
])
def test_05_to_08_decimal_estimate_rates_caps_and_proof(public_bundle, data, application_type,
                                                       cost, expected, cap, categories):
    result = run(public_bundle, data, application_type=application_type,
                 eligible_cost_twd=cost, qualification_categories=categories)
    estimate = result["estimate"]
    assert estimate["available"] is True
    assert Decimal(estimate["estimated_subsidy_twd"]) == Decimal(expected)
    assert Decimal(estimate["cap_twd"]) == Decimal(cap)
    assert estimate["conditional"] is True
    assert estimate["label"] == "依填答試算"
    assert estimate["rounding"] == "not_applied"
    if application_type != "standard":
        assert estimate["proof_pending"] is True
        assert "D06" in documents(result)
    else:
        assert "D06" not in documents(result)


def test_09_multiple_enhanced_categories_do_not_stack(public_bundle, data):
    result = run(public_bundle, data, application_type="specific", eligible_cost_twd="10000",
                 qualification_categories=["low_income", "disability", "language_hakka"])
    assert Decimal(result["estimate"]["estimated_subsidy_twd"]) == Decimal("6000")
    assert Decimal(result["estimate"]["rate"]) == Decimal("0.90")
    assert len([d for d in result["documents"] if d["doc_id"] == "D06"]) == 1


@pytest.mark.parametrize("purchase_date,outcome", [
    ("2026-04-02", "no_issue"), ("2026-10-31", "no_issue"),
    ("2026-04-01", "action_needed"), ("2026-11-01", "action_needed"),
])
def test_10_11_purchase_date_boundaries(public_bundle, data, purchase_date, outcome):
    assert check(run(public_bundle, data, purchase_date=purchase_date), "purchase_date")["outcome"] == outcome


def test_12_april_purchase_not_excluded_by_august_eligibility_update(public_bundle, data):
    result = run(public_bundle, data, purchase_date="2026-04-15")
    assert check(result, "purchase_date")["outcome"] == "no_issue"
    assert check(result, "purchase_deadline")["outcome"] == "manual_review"


def test_13_listed_tool_checks_plan_channel_date_independently(public_bundle, data):
    result = run(public_bundle, data, purchase_channel="agent", purchase_date="2026-04-01")
    assert check(result, "tool")["outcome"] == "no_issue"
    assert check(result, "plan")["outcome"] == "manual_review"
    assert check(result, "channel")["outcome"] == "action_needed"
    assert check(result, "purchase_date")["outcome"] == "action_needed"
    assert check(result, "domain")["outcome"] == "manual_review"


def test_14_unlisted_tool_needs_review_without_exclusion(public_bundle, data):
    result = run(public_bundle, data, tool_id=None, tool_name="合成未列名工具")
    assert check(result, "tool")["outcome"] == "manual_review"
    assert "未" in check(result, "tool")["reason"]


@pytest.mark.parametrize("tool_id", ["capcut", "kling", "meitu", "wink", "whee", "senseavatar", "manus"])
def test_15_notice_exclusions_are_program_limits_not_malware(public_bundle, data, tool_id):
    result = run(public_bundle, data, tool_id=tool_id)
    item = check(result, "tool")
    assert item["outcome"] == "action_needed"
    assert "計畫" in item["reason"] and "限制" in item["reason"]
    assert "惡意軟體" not in item["reason"]
    assert item["source"]["kind"] == "official"


@pytest.mark.parametrize("channel,seller", [("agent", "GoingBus"), ("marketplace", "Poe.com")])
def test_16_listed_tool_from_restricted_channel_remains_restricted(public_bundle, data, channel, seller):
    result = run(public_bundle, data, purchase_channel=channel, seller=seller)
    assert check(result, "tool")["outcome"] == "no_issue"
    assert check(result, "channel")["outcome"] == "action_needed"


def test_17_api_topup_is_not_a_normal_subscription(public_bundle, data):
    subscription = run(public_bundle, data)
    topup = run(public_bundle, data, billing_type="credits", billing_component="standalone_credits")
    assert check(subscription, "billing")["outcome"] != "action_needed"
    assert check(topup, "billing")["outcome"] == "action_needed"


def test_18_included_credit_words_never_trigger_automatic_exclusion(public_bundle, data):
    result = run(public_bundle, data, plan_name="合成填答：訂閱內含 Credit Token",
                 billing_component="included_credits")
    assert check(result, "billing")["outcome"] != "action_needed"
    assert check(result, "plan")["outcome"] == "manual_review"
    mixed = run(public_bundle, data, billing_component="mixed")
    assert check(mixed, "billing")["outcome"] == "manual_review"


def test_19_monthly_accumulation_creates_month_specific_documents(public_bundle, data):
    result = run(public_bundle, data, transactions=[
        {"purchase_date": "2026-08-01", "subscription_start": "2026-08-01",
         "subscription_end": "2026-09-01", "eligible_cost_twd": "1000"},
        {"purchase_date": "2026-09-01", "subscription_start": "2026-09-01",
         "subscription_end": "2026-10-01", "eligible_cost_twd": "1000"},
    ])
    assert check(result, "monthly_accumulation")["outcome"] == "manual_review"
    docs = documents(result)
    assert {"D02_tx_1", "D03_tx_1", "D02_tx_2", "D03_tx_2"}.issubset(docs)
    assert docs["D02_tx_1"]["transaction_index"] == 1
    assert docs["D03_tx_2"]["transaction_index"] == 2
    assert check(result, "transaction_1_purchase_date")["outcome"] == "no_issue"


@pytest.mark.parametrize("relationship", ["parent", "spouse", "legal_guardian"])
def test_20_family_proxy_payment_is_review_not_approval_or_rejection(public_bundle, data, relationship):
    result = run(public_bundle, data, payer="other", payer_relationship=relationship)
    assert check(result, "payer_relationship")["outcome"] == "manual_review"
    assert {"D07_joint", "D07_relationship"}.issubset(documents(result))


def test_21_receiving_account_stays_applicants_own(public_bundle, data):
    result = run(public_bundle, data, payer="other", payer_relationship="parent")
    bank = documents(result)["D04"]
    assert "申請人本人" in bank["title"]
    assert "代付" in bank["reason"] and "本人" in bank["reason"]
    assert bank["required"] is True


def test_22_funding_availability_is_unknown(public_bundle, data):
    result = run(public_bundle, data)
    assert result["funding_status"] == "unknown"
    assert "名額已確認" not in result["summary"]["message"]


def test_23_foreign_currency_only_does_not_fabricate_twd(public_bundle, data):
    result = run(public_bundle, data, eligible_cost_twd=None, foreign_currency_only=True)
    assert result["estimate"]["available"] is False
    assert result["estimate"]["estimated_subsidy_twd"] is None
    assert result["estimate"]["eligible_cost_twd"] is None
    assert check(result, "amount")["outcome"] == "manual_review"
    assert "D03" in documents(result)


def test_24_precheck_has_no_formal_application_effects(public_bundle, data):
    # API integration additionally asserts persisted Case/Task/Decision rows are unchanged.
    result = run(public_bundle, data)
    assert result["snapshot"]["automatic_approval_enabled"] is False
    assert result["snapshot"]["agency_approved"] is False
    text = " ".join(result["disclaimers"])
    assert "不等於正式送件" in text
    assert "不保留期限或補助額度" in text
    assert "government_case_id" not in result and "approved_subsidy" not in result


def test_catalog_exact_examples_no_fabricated_plans_or_domains(public_bundle):
    listed = [t for t in public_bundle.tools if t.catalog_status == "listed_example"]
    excluded = [t for t in public_bundle.tools if t.catalog_status == "excluded_by_notice"]
    assert len(listed) == 18 and len(excluded) == 7
    assert {"Adobe Firefly", "其他 Adobe AI 創作工具"}.issubset({t.name for t in listed})
    assert all(not t.plans and not t.domains for t in public_bundle.tools)
    assert all(t.checked_at.isoformat() == "2026-09-19" for t in public_bundle.tools)
    assert len(public_bundle.pending_confirmations) >= 8


def test_prepared_document_never_becomes_received_or_reviewed(public_bundle, data):
    result = run(public_bundle, data, prepared_documents=["D01", "D02", "D04"])
    for document in result["documents"]:
        assert document["status"] in {"required", "self_reported_ready"}
        assert not document["received"] and not document["reviewed"] and not document["verified"]
        assert document["content_unverified"]
    assert documents(result)["D01"]["status"] == "self_reported_ready"


def test_unknown_qualification_and_non_card_payment_require_review(public_bundle, data):
    result = run(public_bundle, data, application_type="specific", qualification_categories=["synthetic_unknown"],
                 payment_method="other")
    assert check(result, "application_type")["outcome"] == "manual_review"
    assert check(result, "payment_method")["outcome"] == "manual_review"


def test_checks_keep_traceable_rule_version_sources_and_time(public_bundle, data):
    result = run(public_bundle, data)
    for item in result["checks"]:
        assert item["rule_id"] and isinstance(item["related_fields"], list)
        # R08 uses the injected execution timestamp, rather than an applicant field.
        if item["check_id"] != "acceptance_window":
            assert item["related_fields"]
        assert item["rule_version"] == "public_2026_09_19"
        assert item["source"]["url"] == public_bundle.rules.source.url
        assert item["executed_at"] == result["executed_at"]
        assert item["reason"] and item["next_step"] and item["evidence_type"]
        if item["execution_status"] in {"pending", "failed"}:
            assert item["outcome"] is None
