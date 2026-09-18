import secrets

import pyotp
from sqlalchemy import select

from app.auth_crypto import encrypt_totp_secret, hash_password
from app.db import new_id, utcnow
from app.models import Account, ContentVersion, RoleGrant, Scheme


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


def create_staff(db, settings, email, role, password=None):
    if role not in {"reviewer", "supervisor", "admin", "auditor"}:
        raise ValueError("Invalid staff role")
    if db.scalar(select(Account).where(Account.email == email.strip().lower())):
        raise ValueError("Account already exists; credentials were not overwritten")
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
                     scope_id=None if role == "admin" else "youth-demo"))
    db.flush()
    return {"account_id": account.id, "email": account.email, "password": password,
            "totp_secret": secret,
            "totp_uri": pyotp.TOTP(secret).provisioning_uri(account.email, "Youth Service")}
