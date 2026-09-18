"""Live development-only HTTP workflow against the local PostgreSQL service.

Creates identifiable synthetic records, not production users. No credentials or
verification codes are printed. Run from the repository root with the venv.
"""
from datetime import timedelta
import atexit
from concurrent.futures import ThreadPoolExecutor
from email import policy
from email.parser import BytesParser
import io
import re
from uuid import uuid4

import httpx
from PIL import Image
import pyotp

from app.bootstrap import create_staff
from app.config import Settings
from app.db import make_engine, make_session_factory, utcnow
from app.models import Account, AuthSession, Scheme
from app.worker import run_once
from sqlalchemy import select


def main():
    settings = Settings()
    if settings.app_env != "development" or settings.mail_backend != "spool":
        raise SystemExit("Smoke requires development mode and local mail spool.")
    if settings.line_channel_access_token:
        raise SystemExit("Smoke refuses to run a worker configured for external LINE delivery.")
    origin = "http://127.0.0.1:8000"
    run_id = uuid4().hex[:12]
    email = f"smoke-youth-{run_id}@example.org"
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    if engine.dialect.name != "postgresql":
        raise SystemExit("Live smoke requires the migrated PostgreSQL database.")
    with factory() as db:
        staff = create_staff(db, settings, f"smoke-staff-{run_id}@example.org", "supervisor")
        rule_id = db.get(Scheme, "youth-demo").config["rule_version_id"]
        db.commit()
    account_ids = [staff["account_id"]]

    def disable_test_accounts():
        with factory() as db:
            for account_id in account_ids:
                account = db.get(Account, account_id)
                account.status = "DISABLED"
                account.role_version += 1
                for session in db.scalars(select(AuthSession).where(AuthSession.account_id == account_id)):
                    session.revoked_at = utcnow()
            db.commit()

    # A failed assertion must not leave synthetic staff credentials active.
    atexit.register(disable_test_accounts)

    def call(client, method, path, *, body=None, tag=None, key=None, expected=200, **kwargs):
        headers = kwargs.pop("headers", {})
        if tag:
            headers["If-Match"] = tag
        if key:
            headers["Idempotency-Key"] = key
        response = client.request(method, path, json=body, headers=headers, **kwargs)
        statuses = {expected} if isinstance(expected, int) else set(expected)
        assert response.status_code in statuses, f"{method} {path}: {response.status_code} {response.text}"
        return response

    with httpx.Client(base_url=origin, headers={"Origin": origin}, timeout=30) as youth, \
            httpx.Client(base_url=origin, headers={"Origin": origin}, timeout=30) as reviewer:
        call(youth, "GET", "/health/ready")
        challenge = call(youth, "POST", "/api/v1/auth/email/challenges", body={"email": email}, expected=202)
        code = None
        for path in settings.mail_spool_dir.glob("*.eml"):
            message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
            if message["To"] == email:
                code = re.search(r"驗證碼：([0-9]{6})", message.get_body(preferencelist=("plain",)).get_content()).group(1)
        assert code, "No development email received"
        signed_in = call(youth, "POST", "/api/v1/auth/email/challenges/verify", body={
            "challenge_id": challenge.json()["data"]["challenge_id"], "code": code})
        youth.headers["X-CSRF-Token"] = signed_in.json()["data"]["csrf_token"]
        youth_id = signed_in.json()["data"]["account"]["id"]
        account_ids.append(youth_id)
        password_step = call(reviewer, "POST", "/api/v1/auth/staff/session", body={
            "identifier": staff["email"], "password": staff["password"]})
        mfa = call(reviewer, "POST", "/api/v1/auth/staff/mfa", body={
            "mfa_challenge_id": password_step.json()["data"]["mfa_challenge_id"],
            "code": pyotp.TOTP(staff["totp_secret"]).now()})
        reviewer.headers["X-CSRF-Token"] = mfa.json()["data"]["csrf_token"]

        created = call(youth, "POST", "/api/v1/cases", body={"scheme_id": "youth-demo"},
                       key=str(uuid4()), expected=201)
        case_id = created.json()["data"]["id"]
        case_path = f"/api/v1/cases/{case_id}"
        saved = call(youth, "PATCH", case_path, body={"form_data": {
            "name": "開發煙霧測試", "email": email, "subject": f"Live PostgreSQL smoke {run_id}"}},
            tag=created.headers["etag"])
        key = str(uuid4())
        def submit_concurrently(_):
            return call(youth, "POST", case_path + "/submit", body={"file_version_ids": []},
                        tag=saved.headers["etag"], key=key, expected=201)

        with ThreadPoolExecutor(max_workers=2) as pool:
            submitted, parallel_replay = list(pool.map(submit_concurrently, range(2)))
        assert submitted.json()["data"]["receipt"] == parallel_replay.json()["data"]["receipt"]
        replay = call(youth, "POST", case_path + "/submit", body={"file_version_ids": []},
                      tag=saved.headers["etag"], key=key, expected=201)
        assert replay.json()["data"]["receipt"] == submitted.json()["data"]["receipt"]
        staff_path = f"/api/v1/staff/cases/{case_id}"
        call(reviewer, "POST", staff_path + "/start-review", tag=submitted.headers["etag"])
        task = call(reviewer, "POST", staff_path + "/tasks", body={
            "title": "付款證明測試", "requirement": "請補開發測試附件", "acceptance_criteria": "圖片可讀",
            "due_at": (utcnow() + timedelta(days=2)).isoformat()}, key=str(uuid4()), expected=201).json()["data"]
        buffer = io.BytesIO()
        Image.new("RGB", (24, 24), "white").save(buffer, format="PNG")
        content = buffer.getvalue()
        upload = call(youth, "POST", "/api/v1/files/upload-intents", body={
            "case_id": case_id, "task_id": task["id"], "file_name": "smoke-proof.png",
            "content_type": "image/png", "size_bytes": len(content), "document_type": "PAYMENT_PROOF"},
            expected=201).json()["data"]
        call(youth, "PUT", upload["upload_url"], content=content, headers=upload["upload_headers"])
        call(youth, "POST", f"/api/v1/files/{upload['file_id']}/complete",
             body={"file_version_id": upload["file_version_id"]})
        supplemented = call(youth, "POST", f"/api/v1/tasks/{task['id']}/submissions", body={
            "task_revision": task["task_revision"], "file_version_ids": [upload["file_version_id"]]},
            tag=task["etag"], key=str(uuid4()), expected=201)
        accepted_body = {"submission_id": supplemented.json()["data"]["receipt"]["submission_id"],
                         "review_note": "開發測試圖片已核對"}
        early_accept = call(reviewer, "POST", f"/api/v1/staff/tasks/{task['id']}/accept", body=accepted_body,
                            tag=supplemented.headers["etag"], expected={200, 409})
        if early_accept.status_code == 409:
            assert early_accept.json()["error"]["code"] == "FILE_NOT_CLEAN"
        run_once(factory, settings)
        if early_accept.status_code != 200:
            call(reviewer, "POST", f"/api/v1/staff/tasks/{task['id']}/accept", body=accepted_body,
                 tag=supplemented.headers["etag"])
        evidence = [{"file_version_id": upload["file_version_id"], "rule_version_id": rule_id}]
        items = call(reviewer, "GET", staff_path + "/review-items").json()["data"]["items"]
        call(reviewer, "PATCH", staff_path + f"/review-items/{items[0]['id']}", body={
            "result": "PASS", "evidence_refs": evidence}, tag=items[0]["etag"])
        current = call(reviewer, "GET", staff_path)
        decision = call(reviewer, "POST", staff_path + "/decisions", body={
            "outcome": "APPROVED", "reason": "開發煙霧流程核准", "rule_version_id": rule_id,
            "evidence_refs": evidence}, tag=current.headers["etag"], key=str(uuid4()), expected=201)
        closed = call(reviewer, "POST", staff_path + "/close", body={"completion_note": "開發流程完成"},
                      tag=decision.headers["etag"])
        assert closed.json()["data"]["status"] == "CLOSED"
        export = call(reviewer, "POST", "/api/v1/exports", body={"scheme_id": "youth-demo",
                      "purpose": "開發煙霧流程匯出測試"}, expected=202).json()["data"]
        run_once(factory, settings)
        downloaded = call(reviewer, "GET", f"/api/v1/exports/{export['id']}/download")
        assert "text/csv" in downloaded.headers["content-type"]
        # Retain synthetic case evidence but immediately disable test credentials.
        disable_test_accounts()
        atexit.unregister(disable_test_accounts)
        assert youth.get(case_path).status_code == 401
        print(f"PASS live PostgreSQL HTTP workflow; synthetic case {case_id} CLOSED; test accounts disabled.")
    engine.dispose()


if __name__ == "__main__":
    main()
