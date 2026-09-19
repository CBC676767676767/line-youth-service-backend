import secrets

import pyotp
from sqlalchemy import select

from app.auth_crypto import encrypt_totp_secret, hash_password
from app.db import new_id, utcnow
from app.models import Account, ContentVersion, RoleGrant, Scheme

GRANT_SCHEME_ID = "hsinchu-ai-grant-2026"
GRANT_SOURCE = "https://dgservice.hccg.gov.tw/serviceNotice.do?id=1323&rule=guest"
GRANT_CRITERIA = {
    "eligibility": "申請資格與期限",
    "documents": "文件完整性與清晰度",
    "expense": "購買項目與申請金額",
    "identity": "本人、付款與收款資料",
    "safety": "重複補助與其他人工查核",
}
GRANT_DOCUMENTS = ["ID_FRONT", "ID_BACK", "RECEIPT", "PAYMENT_PROOF", "BANK_ACCOUNT", "AFFIDAVIT",
                   "SPECIAL_STATUS", "RELATIONSHIP"]


def grant_form_schema():
    """Validate input structure, never determine eligibility or an award amount."""
    titles = {
        "name": "姓名", "email": "電子信箱", "birth": "出生日期", "city": "戶籍所在縣市",
        "tool": "AI 工具完整名稱", "channel": "購買管道", "plan": "方案類型",
        "purchaseDate": "購買日期", "periodEnd": "訂閱到期／續訂日期",
        "amount": "臺幣帳單實付金額", "requested": "申請補助金額", "receiptName": "憑證姓名",
        "receiptEmail": "憑證電子信箱", "payer": "付款人", "special": "申請特定對象補助",
        "bankName": "存摺戶名", "receiptAmount": "憑證核對臺幣金額", "phone": "聯絡電話",
        "address": "戶籍／通訊地址", "identityHint": "遮罩身分證字號", "company": "軟體公司",
        "origin": "開發／營運地區", "currency": "原始費用幣別", "originalAmount": "收據原始費用",
        "paymentMethod": "付款方式", "bankType": "收款銀行類別", "category": "工具功能類別",
    }
    required = ["name", "email", "birth", "city", "tool", "purchaseDate", "amount", "requested",
                "channel", "plan", "payer", "special", "paymentMethod"]
    properties = {key: {"title": title, "type": "string", "maxLength": 500}
                  for key, title in titles.items()}
    for key in required:
        properties[key]["minLength"] = 1
    properties["special"] = {"title": titles["special"], "type": "boolean"}
    for key, values in {
        "channel": ["official", "reseller", "unknown"],
        "plan": ["monthly", "annual", "credits"],
        "payer": ["self", "relative"],
        "paymentMethod": ["card", "telecom", "wallet", "other"],
        "bankType": ["taiwan", "other", ""],
        "category": ["通用型", "影像類", "辦公類", "學習類", "其他類", ""],
        "currency": ["USD", "TWD", "EUR", "JPY", "其他", ""],
    }.items():
        properties[key]["enum"] = values
    for key in ("birth", "purchaseDate", "periodEnd"):
        valid_date = {"format": "date", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
        if key in required:
            properties[key].update(valid_date)
        else:
            properties[key]["anyOf"] = [{"const": ""}, valid_date]
    for key in ("amount", "requested", "receiptAmount", "originalAmount"):
        # Technical representation only: no percentage, cap, or matching-name decisions here.
        pattern = r"^(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,2})?$"
        properties[key]["pattern"] = pattern if key in required else "^$|" + pattern
    properties["email"].update({"format": "email", "maxLength": 254})
    properties["receiptEmail"].update({"maxLength": 254, "anyOf": [{"const": ""}, {"format": "email"}]})
    properties["identityHint"]["maxLength"] = 30
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
            "additionalProperties": False, "properties": properties, "required": required}


def seed_grant_scheme(db):
    """Add the grant as a separate versioned scheme; never rewrite operator changes."""
    existing = db.get(Scheme, GRANT_SCHEME_ID)
    if existing:
        return existing
    rule_id = new_id()
    scheme = Scheme(
        id=GRANT_SCHEME_ID, name="新竹市青年 AI 工具補助（2026 整合試辦）",
        description="依 2026 年公開計畫建立的申請與人工審查流程，尚未經機關驗收或串接官方收件。",
        schema_version=1, form_schema=grant_form_schema(),
        config={
            "rule_version_id": rule_id, "required_criteria": list(GRANT_CRITERIA),
            "criterion_labels": GRANT_CRITERIA,
            "allow_late_submission": False, "allow_withdrawal": True,
            "allow_empty_draft_fields": True, "document_types": GRANT_DOCUMENTS,
            "required_document_types": GRANT_DOCUMENTS[:6],
            "conditional_document_requirements": [
                {"field": "special", "equals": True, "document_types": ["SPECIAL_STATUS"]},
                {"field": "payer", "equals": "relative", "document_types": ["RELATIONSHIP"]},
            ],
            "source_url": GRANT_SOURCE,
        },
    )
    db.add(scheme)
    db.add(ContentVersion(
        id=rule_id, kind="RULE", code=scheme.id, version_no=1, status="PUBLISHED", published_at=utcnow(),
        body={"name": "2026 新竹市青年 AI 工具補助人工審查參考規則",
              "criteria": list(GRANT_CRITERIA), "criterion_labels": GRANT_CRITERIA,
              "source_url": GRANT_SOURCE, "source_amended_on": "2026-08-14",
              "description": "依公開 2026 規則建模，未經機關驗收。發布僅表示本系統可引用版本，"
                             "不代表官方核准、身分驗證或資格自動判定。",
              "manual_checks": ["資格、設籍與申請期限", "清晰文件與親筆簽名", "訂閱與臺幣付款依據",
                                "本人帳戶及代付例外", "特定對象證明、重複補助及其他機關查核"],
              "unresolved": ["代付適用關係的不同表述", "月繳累積與跨月及申請期限",
                             "金額尾數處理", "補正工作天起算與機關行事曆"]},
    ))
    db.flush()
    return scheme


def seed_scheme(db):
    existing = db.get(Scheme, "youth-demo")
    if existing:
        return existing
    rule_id = new_id()
    scheme = Scheme(
        id="youth-demo", name="青年服務示範方案（開發測試用）",
        description="示範申請及補件流程；不代表任何實際補助資格。",
        schema_version=1,
        form_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object", "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 100},
                "email": {"type": "string", "format": "email", "maxLength": 254},
                "phone": {"type": "string", "maxLength": 30},
                "subject": {"type": "string", "minLength": 1, "maxLength": 200},
                "description": {"type": "string", "maxLength": 2000},
            },
            "required": ["name", "email", "subject"],
        },
        config={"rule_version_id": rule_id, "required_criteria": ["eligibility"],
                "allow_late_submission": False, "allow_withdrawal": True,
                "document_types": ["PAYMENT_PROOF", "APPLICATION", "OTHER"]},
    )
    db.add(scheme)
    db.add(ContentVersion(id=rule_id, kind="RULE", code=scheme.id, version_no=1,
                          body={"name": "開發測試規則", "criteria": ["eligibility"],
                                "description": "承辦依測試情境檢核，不代表正式補助規則。"},
                          status="PUBLISHED", published_at=utcnow()))
    db.flush()
    return scheme


def create_staff(db, settings, email, role, password=None, *, scheme_id="youth-demo"):
    if role not in {"reviewer", "supervisor", "admin", "auditor"}:
        raise ValueError("Invalid staff role")
    if db.scalar(select(Account).where(Account.email == email.strip().lower())):
        raise ValueError("Account already exists; credentials were not overwritten")
    if role != "admin" and db.get(Scheme, scheme_id) is None:
        raise ValueError("Scheme does not exist; seed it before creating staff")
    password = password or secrets.token_urlsafe(20)
    secret = pyotp.random_base32()
    account = Account(email=email.strip().lower(), email_verified_at=utcnow(),
                      password_hash=hash_password(password),
                      totp_secret_encrypted=encrypt_totp_secret(
                          secret, settings.secret_key, settings.totp_encryption_key))
    db.add(account)
    db.flush()
    db.add(RoleGrant(account_id=account.id, role=role,
                     scope_type="GLOBAL" if role == "admin" else "SCHEME",
                     scope_id=None if role == "admin" else scheme_id))
    db.flush()
    return {"account_id": account.id, "email": account.email, "password": password,
            "totp_secret": secret,
            "totp_uri": pyotp.TOTP(secret).provisioning_uri(account.email, "Youth Service")}
