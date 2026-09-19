"""Grant contract, server-side submission requirements and attachment visibility."""

from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.bootstrap import GRANT_CRITERIA, GRANT_DOCUMENTS, GRANT_SCHEME_ID, create_staff, seed_grant_scheme
from app.cases import _validate_form
from app.common import ApiError
from app.models import Account, ContentVersion, RoleGrant, Scheme
from test_cases import PDF_BYTES, workflow as workflow


FORM = {
    "name": "合成申請人", "email": "owner@example.test", "birth": "2001-05-16", "city": "新竹市",
    "tool": "ChatGPT", "channel": "official", "plan": "monthly", "purchaseDate": "2026-09-05",
    "periodEnd": "2026-10-05", "amount": "680", "requested": "340", "receiptName": "合成申請人",
    "receiptEmail": "owner@example.test", "payer": "self", "special": False, "bankName": "合成申請人",
    "receiptAmount": "680", "phone": "0900-000-000", "address": "新竹市東區", "identityHint": "",
    "company": "OpenAI", "origin": "美國", "currency": "USD", "originalAmount": "20",
    "paymentMethod": "card", "bankType": "taiwan", "category": "通用型",
}


@pytest.fixture
def grant(workflow):
    with workflow.factory() as db:
        seed_grant_scheme(db)
        db.commit()
    return workflow


def draft(env, form=None):
    response = env.call("owner", "POST", "/cases", {"scheme_id": GRANT_SCHEME_ID}, key=str(uuid4()))
    assert response.status_code == 201, response.text
    case_id = response.json()["data"]["id"]
    response = env.call("owner", "PATCH", f"/cases/{case_id}", {"form_data": form or FORM},
                        etag=response.headers["etag"])
    assert response.status_code == 200, response.text
    return case_id, response.headers["etag"]


def upload_document(env, case_id, document_type, *, complete=True):
    response = env.call("owner", "POST", "/files/upload-intents", {
        "case_id": case_id, "file_name": "synthetic.pdf", "content_type": "application/pdf",
        "size_bytes": len(PDF_BYTES), "document_type": document_type,
    })
    assert response.status_code == 201, response.text
    intent = response.json()["data"]
    if complete:
        response = env.call("owner", "PUT", intent["upload_url"],
                            headers=intent["upload_headers"], content=PDF_BYTES)
        assert response.status_code == 200, response.text
        response = env.call("owner", "POST", f"/files/{intent['file_id']}/complete",
                            {"file_version_id": intent["file_version_id"]})
        assert response.status_code == 200, response.text
    return intent


def test_seed_is_separate_idempotent_and_never_overwrites(grant):
    with grant.factory() as db:
        original = deepcopy(db.get(Scheme, "youth-demo").form_schema)
        scheme = seed_grant_scheme(db)
        rule_id = scheme.config["rule_version_id"]
        scheme.description = "operator-maintained text"
        db.commit()
        again = seed_grant_scheme(db)
        assert again.description == "operator-maintained text"
        assert again.config["rule_version_id"] == rule_id
        assert db.get(Scheme, "youth-demo").form_schema == original
        assert db.scalar(select(func.count()).select_from(ContentVersion).where(
            ContentVersion.code == GRANT_SCHEME_ID)) == 1


def test_published_contract_exposes_rule_criteria_and_field_titles(grant):
    response = grant.call("owner", "GET", f"/schemes/{GRANT_SCHEME_ID}")
    contract = response.json()["data"]
    assert contract["required_criteria"] == list(GRANT_CRITERIA)
    assert contract["criterion_labels"] == GRANT_CRITERIA
    assert contract["required_document_types"] == GRANT_DOCUMENTS[:6]
    assert len(contract["conditional_document_requirements"]) == 2
    schema = grant.call("owner", "GET", f"/schemes/{GRANT_SCHEME_ID}/form-schema").json()["data"]
    assert schema["rule_version_id"] == contract["rule_version_id"]
    assert set(schema["form_schema"]["properties"]) == set(FORM)
    assert all(field.get("title") for field in schema["form_schema"]["properties"].values())
    assert grant.call("owner", "GET", "/schemes/youth-demo").json()["data"]["rule_version_id"]
    with grant.factory() as db:
        db.get(ContentVersion, contract["rule_version_id"]).status = "DRAFT"
        db.commit()
    assert grant.call("owner", "GET", f"/schemes/{GRANT_SCHEME_ID}").json()["data"]["rule_version_id"] is None


@pytest.mark.parametrize("key,value", [
    ("amount", 680), ("requested", "1e3"), ("amount", "-1"), ("amount", "1.234"),
    ("birth", "2001-02-29"), ("purchaseDate", "2026-13-01"), ("periodEnd", "not-a-date"),
    ("email", "not-an-email"), ("special", "true"), ("payer", "friend"),
    ("paymentMethod", "unrecognized"), ("status", "APPROVED"),
])
def test_schema_rejects_malformed_input(grant, key, value):
    with grant.factory() as db:
        with pytest.raises(ApiError) as error:
            _validate_form(db.get(Scheme, GRANT_SCHEME_ID), {**FORM, key: value}, draft=False)
        assert error.value.code == "FORM_INVALID"


def test_drafts_allow_empty_fields_but_submission_requires_basics(grant):
    form = {key: False if key == "special" else "" for key in FORM}
    case_id, tag = draft(grant, form)
    result = grant.call("owner", "POST", f"/cases/{case_id}/submit", {"file_version_ids": []},
                        key=str(uuid4()), etag=tag)
    assert result.status_code == 422
    assert result.json()["error"]["code"] == "FORM_INVALID"


@pytest.mark.parametrize("special,payer,expected", [
    (False, "self", set()), (True, "self", {"SPECIAL_STATUS"}),
    (False, "relative", {"RELATIONSHIP"}), (True, "relative", {"SPECIAL_STATUS", "RELATIONSHIP"}),
])
def test_required_and_conditional_documents_are_enforced_by_server(grant, special, payer, expected):
    case_id, tag = draft(grant, {**FORM, "special": special, "payer": payer})
    submitted = []

    def submit():
        return grant.call("owner", "POST", f"/cases/{case_id}/submit", {"file_version_ids": submitted},
                          key=str(uuid4()), etag=tag)

    assert submit().json()["error"]["code"] == "REQUIRED_DOCUMENT_MISSING"
    for doc in GRANT_DOCUMENTS[:6]:
        submitted.append(upload_document(grant, case_id, doc)["file_version_id"])
    if expected:
        response = submit()
        assert response.status_code == 422
        assert {item["message"].split("：")[-1] for item in response.json()["error"]["field_errors"]} == expected
        assert grant.call("owner", "GET", f"/cases/{case_id}").json()["data"]["status"] == "DRAFT"
        for doc in sorted(expected):
            submitted.append(upload_document(grant, case_id, doc)["file_version_id"])
    response = submit()
    assert response.status_code == 201, response.text
    assert response.json()["data"]["case_status"] == "RECEIVED"
    response = grant.call("supervisor", "POST", f"/staff/cases/{case_id}/start-review",
                          etag=response.headers["etag"])
    assert response.status_code == 200
    items = grant.call("supervisor", "GET", f"/staff/cases/{case_id}/review-items").json()["data"]["items"]
    assert {item["criterion_code"] for item in items} == set(GRANT_CRITERIA)
    assert all(item["result"] == "PENDING" for item in items)


def test_schema_does_not_auto_decide_substantive_eligibility(grant):
    with grant.factory() as db:
        # These require an accountable human decision, not a schema/automatic rejection.
        _validate_form(db.get(Scheme, GRANT_SCHEME_ID), {
            **FORM, "city": "新竹縣", "birth": "1970-01-01", "tool": "未列於範例的新工具",
            "channel": "reseller", "plan": "credits", "requested": "99000", "receiptName": "不同姓名",
            "purchaseDate": "2025-01-01",
        }, draft=False)


def test_document_allowlist_and_case_file_metadata_visibility(grant):
    case_id, tag = draft(grant)
    rejected = grant.call("owner", "POST", "/files/upload-intents", {
        "case_id": case_id, "file_name": "wrong.pdf", "content_type": "application/pdf",
        "size_bytes": len(PDF_BYTES), "document_type": "OTHER",
    })
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "DOCUMENT_TYPE_NOT_ALLOWED"
    completed = [upload_document(grant, case_id, doc) for doc in GRANT_DOCUMENTS[:6]]
    unused = upload_document(grant, case_id, "RECEIPT")
    upload_document(grant, case_id, "RECEIPT", complete=False)
    applicant = grant.call("owner", "GET", f"/cases/{case_id}").json()["data"]
    assert len(applicant["files"]) == 7
    assert unused["file_version_id"] in {f["file_version_id"] for f in applicant["files"]}
    assert all("object_key" not in f and "task_id" in f for f in applicant["files"])
    assert grant.call("other", "GET", f"/cases/{case_id}").status_code == 404
    assert grant.call("supervisor", "GET", f"/staff/cases/{case_id}").json()["data"]["files"] == []
    response = grant.call("owner", "POST", f"/cases/{case_id}/submit",
                          {"file_version_ids": [f["file_version_id"] for f in completed]},
                          key=str(uuid4()), etag=tag)
    assert response.status_code == 201
    staff_files = grant.call("supervisor", "GET", f"/staff/cases/{case_id}").json()["data"]["files"]
    assert {f["file_version_id"] for f in staff_files} == {f["file_version_id"] for f in completed}


def test_staff_scheme_scope_is_explicit_and_existing_accounts_unchanged(grant):
    with grant.factory() as db:
        created = create_staff(db, grant.settings, "grant-staff@example.test", "supervisor",
                               scheme_id=GRANT_SCHEME_ID)
        scoped = db.scalar(select(RoleGrant).where(RoleGrant.account_id == created["account_id"]))
        assert (scoped.scope_type, scoped.scope_id) == ("SCHEME", GRANT_SCHEME_ID)
        default = create_staff(db, grant.settings, "demo-staff@example.test", "reviewer")
        assert db.scalar(select(RoleGrant.scope_id).where(RoleGrant.account_id == default["account_id"])) == "youth-demo"
        with pytest.raises(ValueError, match="already exists"):
            create_staff(db, grant.settings, "grant-staff@example.test", "supervisor", scheme_id="youth-demo")
        assert db.scalar(select(func.count()).select_from(RoleGrant).where(
            RoleGrant.account_id == created["account_id"])) == 1
        with pytest.raises(ValueError, match="Scheme does not exist"):
            create_staff(db, grant.settings, "missing@example.test", "reviewer", scheme_id="missing")
        assert db.scalar(select(Account).where(Account.email == "missing@example.test")) is None

def test_submission_refuses_a_tool_the_public_notice_excludes(grant):
    """The entry page warns earlier; only the server sees every submission."""
    case_id, tag = draft(grant, {**FORM, "tool": "CapCut Pro"})
    response = grant.call("owner", "POST", f"/cases/{case_id}/submit", {"file_version_ids": []},
                          key=str(uuid4()), etag=tag)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "TOOL_NOT_ELIGIBLE"
    # The refusal quotes the notice and says where the wording came from, so the
    # applicant can check it rather than take the system's word for it.
    quoted = " ".join(item["message"] for item in error["field_errors"])
    assert "中國大陸（含港澳）" in quoted
    assert "查核日期" in quoted
    assert all(item["field"] == "tool" for item in error["field_errors"])

    # A tool the notice does not name reaches the document check instead.
    case_id, tag = draft(grant, {**FORM, "tool": "ChatGPT Plus"})
    allowed = grant.call("owner", "POST", f"/cases/{case_id}/submit", {"file_version_ids": []},
                         key=str(uuid4()), etag=tag)
    assert allowed.json()["error"]["code"] == "REQUIRED_DOCUMENT_MISSING"
