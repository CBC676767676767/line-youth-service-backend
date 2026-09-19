"""Isolated loopback fixture for real browser/API tests, never a production service.

All credentials and OTPs stay in mode-0600 files. The fixture adds no login bypass.
Only the development file scanner runs; no notification/SMTP/LINE worker is started.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import timedelta
from email import policy
from email.parser import BytesParser
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import threading
import time
from uuid import uuid4

from cryptography.fernet import Fernet
import httpx
from PIL import Image, ImageDraw
import pyotp
import uvicorn

from app.bootstrap import GRANT_DOCUMENTS, GRANT_SCHEME_ID, create_staff, seed_grant_scheme
from app.config import Settings
from app.db import Base, make_engine, make_session_factory, utcnow
from app.main import create_app
from app.worker import process_scans


REPO = Path(__file__).resolve().parent.parent
FORMAT = "youth-isolated-web-test-v1"


def private_json(path: Path, value, *, replace=False):
    """Write credentials without following symlinks or displaying their contents."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if replace:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        private_json(temporary, value)
        os.replace(temporary, path)
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def synthetic_form(email):
    return {
        "name": "整合測試申請人", "email": email, "birth": "2001-05-16", "city": "新竹市",
        "tool": "ChatGPT", "channel": "official", "plan": "monthly", "purchaseDate": "2026-09-05",
        "periodEnd": "2026-10-05", "amount": "680", "requested": "340", "receiptName": "整合測試申請人",
        "receiptEmail": email, "payer": "self", "special": False, "bankName": "整合測試申請人",
        "receiptAmount": "680", "phone": "0900000000", "address": "新竹市東區測試路1號",
        "identityHint": "A•••••••••", "company": "OpenAI", "origin": "美國", "currency": "USD",
        "originalAmount": "20", "paymentMethod": "card", "bankType": "taiwan", "category": "通用型",
    }


@contextmanager
def fixture(port: int, state_file: Path | None = None):
    if not 1024 <= port <= 65535:
        raise ValueError("Use a loopback port between 1024 and 65535.")
    if state_file is not None:
        state_file = state_file.absolute()
        if state_file.resolve().is_relative_to(REPO):
            raise ValueError("Credentials must stay in a private temporary directory outside the repository.")
        if state_file.exists() or state_file.is_symlink():
            raise ValueError("Refusing to overwrite an existing credential file.")
    with tempfile.TemporaryDirectory(prefix="youth-web-smoke-") as temporary:
        root = Path(temporary).resolve()
        token = uuid4().hex
        origin = f"http://127.0.0.1:{port}"
        settings = Settings(
            _env_file=None, app_env="test", database_url=f"sqlite:///{root / 'test.db'}",
            public_origin=origin, allowed_origins=[origin], cookie_secure=False,
            secret_key=secrets.token_urlsafe(48), totp_encryption_key=Fernet.generate_key().decode(),
            secrets_file=root / "unused-secrets.json", storage_dir=root / "files",
            mail_backend="spool", mail_spool_dir=root / "mail", mail_from="fixture@example.org",
            smtp_host="", smtp_username="", smtp_password="", scan_backend="development",
            line_channel_id="", line_provider_id="", line_messaging_channel_id="",
            line_channel_secret="", line_channel_access_token="", line_destination_user_id="",
            frontend_dist=REPO / "frontend" / "dist", auto_create_schema=False,
        )
        engine = make_engine(settings.database_url)
        factory = make_session_factory(engine)
        # Import/register all router-owned models (including auth_rate_limits) before create_all.
        app = create_app(settings, session_factory=factory)
        Base.metadata.create_all(engine)
        with factory() as db:
            scheme = seed_grant_scheme(db)
            supervisor = create_staff(db, settings, f"browser-supervisor-{token[:10]}@example.org",
                                      "supervisor", scheme_id=GRANT_SCHEME_ID)
            admin = create_staff(db, settings, f"browser-admin-{token[:10]}@example.org",
                                 "admin", scheme_id=GRANT_SCHEME_ID)
            rule_id = scheme.config["rule_version_id"]
            db.commit()
        document_dir = root / "documents"
        document_dir.mkdir(mode=0o700)
        documents = {}
        for kind in [*GRANT_DOCUMENTS, "PAYMENT_PROOF_REVISED"]:
            path = document_dir / f"synthetic-{kind.lower()}.png"
            image = Image.new("RGB", (1200, 800), "white")
            draw = ImageDraw.Draw(image)
            draw.text((55, 80), "SYNTHETIC TEST FILE - NOT A REAL DOCUMENT", fill="black", font_size=30)
            draw.text((55, 200), kind, fill="black", font_size=38)
            draw.text((55, 330), "Local integration test only. No actual identity or payment.",
                      fill="black", font_size=26)
            image.save(path, "PNG")
            path.chmod(0o600)
            documents[kind] = {"path": str(path), "file_name": path.name, "content_type": "image/png"}
        applicant = {"email": f"browser-applicant-{token[:10]}@example.org", "name": "整合測試申請人"}
        state = {
            "format": FORMAT, "fixture_id": token, "pid": os.getpid(), "base_url": origin,
            "data_dir": str(root), "mail_spool_dir": str(settings.mail_spool_dir),
            "codes_file": str(root / "codes.json"), "settings": settings.model_dump(mode="json"),
            "applicant": applicant, "staff": {"supervisor": supervisor, "admin": admin},
            "scheme_id": GRANT_SCHEME_ID, "rule_version_id": rule_id,
            "form": synthetic_form(applicant["email"]), "documents": documents,
        }
        private_json(root / "fixture-marker.json", {"format": FORMAT, "fixture_id": token})
        output = state_file or root / "state.json"
        private_json(output, state)
        @app.get("/__smoke__/identity", include_in_schema=False)
        def identity():
            # Identifies this disposable server, but reveals no credentials or codes.
            return {"fixture_id": token, "test_only": True}

        try:
            yield state, app, settings, factory
        finally:
            engine.dispose()
            output.unlink(missing_ok=True)


def load_state(path: Path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("Fixture credentials must be an owned regular file with mode 0600.")
    state = json.loads(path.read_text(encoding="utf-8"))
    root = Path(state["data_dir"]).resolve()
    marker = json.loads((root / "fixture-marker.json").read_text(encoding="utf-8"))
    if state.get("format") != FORMAT or marker != {"format": FORMAT, "fixture_id": state.get("fixture_id")}:
        raise ValueError("Not an active isolated fixture.")
    settings = Settings(_env_file=None, **state["settings"])
    if (settings.app_env != "test" or settings.mail_backend != "spool" or settings.scan_backend != "development"
            or settings.database_url != f"sqlite:///{root / 'test.db'}"
            or not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{4,5}", state["base_url"])
            or settings.public_origin != state["base_url"]
            or any([settings.smtp_host, settings.line_channel_id, settings.line_channel_access_token,
                    settings.line_channel_secret, settings.line_destination_user_id])):
        raise ValueError("Fixture refused: configuration is not strictly local and isolated.")
    for directory in (settings.storage_dir, settings.mail_spool_dir, Path(state["codes_file"])):
        if not directory.resolve().is_relative_to(root):
            raise ValueError("Fixture path escapes its temporary directory.")
    return state, settings


def read_otp(directory: Path, email: str):
    candidates = sorted(directory.glob("*.eml"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
    for path in candidates:
        message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
        if str(message["To"]) == email:
            body = message.get_body(preferencelist=("plain",))
            match = re.search(r"驗證碼：([0-9]{6})", body.get_content()) if body else None
            if match:
                return match[1]
    return None


def codes(state, settings):
    # This file is read by the test process, never returned by an HTTP endpoint or stdout.
    value = {
        "applicant_otp": read_otp(settings.mail_spool_dir, state["applicant"]["email"]),
        "supervisor_totp": pyotp.TOTP(state["staff"]["supervisor"]["totp_secret"]).now(),
        "admin_totp": pyotp.TOTP(state["staff"]["admin"]["totp_secret"]).now(),
        "generated_at": utcnow().isoformat(),
    }
    private_json(Path(state["codes_file"]), value, replace=True)


def scan(settings):
    engine = make_engine(settings.database_url)
    try:
        return process_scans(make_session_factory(engine), settings)
    finally:
        engine.dispose()


def serve(args):
    with fixture(args.port, args.state_file) as (state, app, settings, factory):
        stop = threading.Event()

        def scans():
            while not stop.wait(args.scan_interval):
                process_scans(factory, settings)

        thread = None
        if args.scan_interval > 0:
            thread = threading.Thread(target=scans, daemon=True)
            thread.start()
        print(f"Isolated fixture prepared at {state['base_url']}; credentials written privately.", flush=True)
        try:
            uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, log_level="critical")
        finally:
            stop.set()
            if thread:
                thread.join(timeout=10)


def verify_server(state):
    with httpx.Client(base_url=state["base_url"], timeout=2, trust_env=False) as client:
        response = client.get("/__smoke__/identity")
        if response.status_code != 200 or response.json() != {
            "fixture_id": state["fixture_id"], "test_only": True,
        }:
            raise RuntimeError("Refusing requests: endpoint is not this isolated fixture.")


def api_workflow(state, settings, factory, *, require_web=False):
    """Exercise actual HTTP, cookies, CSRF, file bytes and business state transitions."""
    verify_server(state)

    def call(client, method, path, *, body=None, tag=None, key=None, expected=200, **kwargs):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Only relative loopback requests are allowed.")
        headers = kwargs.pop("headers", {})
        if tag:
            headers["If-Match"] = tag
        if key:
            headers["Idempotency-Key"] = key
        response = client.request(method, path, json=body, headers=headers, **kwargs)
        if response.status_code != expected:
            # Never dump responses, credentials, cookies, OTPs or submitted form bodies.
            code = response.json().get("error", {}).get("code", "UNKNOWN") if "json" in response.headers.get("content-type", "") else "NON_JSON"
            raise AssertionError(f"{method} {path}: expected {expected}, got {response.status_code} ({code})")
        return response

    def login_staff(client, credentials):
        challenge = call(client, "POST", "/api/v1/auth/staff/session", body={
            "identifier": credentials["email"], "password": credentials["password"],
        }).json()["data"]
        signed_in = call(client, "POST", "/api/v1/auth/staff/mfa", body={
            "mfa_challenge_id": challenge["mfa_challenge_id"],
            "code": pyotp.TOTP(credentials["totp_secret"]).now(),
        }).json()["data"]
        client.headers["X-CSRF-Token"] = signed_in["csrf_token"]
        return signed_in

    def upload(client, case_id, document_type, task_id=None, revised=False):
        document = state["documents"]["PAYMENT_PROOF_REVISED" if revised else document_type]
        content = Path(document["path"]).read_bytes()
        intent = call(client, "POST", "/api/v1/files/upload-intents", expected=201, body={
            "case_id": case_id, "task_id": task_id, "file_name": document["file_name"],
            "document_type": document_type, "content_type": document["content_type"], "size_bytes": len(content),
        }).json()["data"]
        call(client, "PUT", intent["upload_url"], headers=intent["upload_headers"], content=content)
        completed = call(client, "POST", f"/api/v1/files/{intent['file_id']}/complete", body={
            "file_version_id": intent["file_version_id"],
        }).json()["data"]
        assert completed["scan_status"] == "PENDING_SCAN"
        return intent

    options = {"base_url": state["base_url"], "headers": {"Origin": state["base_url"]},
               "timeout": 30, "trust_env": False, "follow_redirects": False}
    with httpx.Client(**options) as youth, httpx.Client(**options) as staff, httpx.Client(**options) as admin:
        assert call(youth, "GET", "/health/ready").json()["data"]["environment"] == "test"
        if require_web:
            public = call(youth, "GET", "/")
            internal = call(staff, "GET", "/admin/")
            assert "text/html" in public.headers["content-type"]
            assert public.text != internal.text
        email = state["applicant"]["email"]
        challenge = call(youth, "POST", "/api/v1/auth/email/challenges", body={"email": email}, expected=202)
        code = read_otp(settings.mail_spool_dir, email)
        assert code is not None, "The private spool did not receive the OTP."
        signed_in = call(youth, "POST", "/api/v1/auth/email/challenges/verify", body={
            "challenge_id": challenge.json()["data"]["challenge_id"], "code": code,
        }).json()["data"]
        assert signed_in["roles"] == ["applicant"]
        youth.headers["X-CSRF-Token"] = signed_in["csrf_token"]
        assert call(staff, "GET", "/api/v1/me", expected=401).json()["error"]["code"]
        assert "supervisor" in login_staff(staff, state["staff"]["supervisor"])["roles"]
        assert "admin" in login_staff(admin, state["staff"]["admin"])["roles"]
        call(admin, "GET", "/api/v1/admin/accounts")
        call(youth, "GET", "/api/v1/staff/cases", expected=403)

        created = call(youth, "POST", "/api/v1/cases", body={"scheme_id": state["scheme_id"]},
                       key=str(uuid4()), expected=201)
        case_id = created.json()["data"]["id"]
        case_path = f"/api/v1/cases/{case_id}"
        staff_path = f"/api/v1/staff/cases/{case_id}"
        saved = call(youth, "PATCH", case_path, body={"form_data": state["form"]}, tag=created.headers["etag"])
        assert saved.json()["data"]["status"] == "DRAFT"
        assert call(youth, "GET", case_path).json()["data"]["form_data"] == state["form"]
        uploads = [upload(youth, case_id, kind) for kind in GRANT_DOCUMENTS[:6]]
        call(youth, "GET", f"/api/v1/files/{uploads[0]['file_id']}/download", expected=409)
        submitted = call(youth, "POST", case_path + "/submit", body={
            "file_version_ids": [item["file_version_id"] for item in uploads],
        }, tag=saved.headers["etag"], key=str(uuid4()), expected=201)
        assert submitted.json()["data"]["case_status"] == "RECEIVED"
        assert len(submitted.json()["data"]["receipt"]["file_version_ids"]) == 6
        detail = call(staff, "GET", staff_path)
        assert len(detail.json()["data"]["files"]) == 6
        revision_id = detail.json()["data"]["revisions"][0]["id"]
        started = call(staff, "POST", staff_path + "/start-review", tag=detail.headers["etag"])
        assert started.json()["data"]["status"] == "UNDER_REVIEW"
        items = call(staff, "GET", staff_path + "/review-items").json()["data"]["items"]
        assert len(items) == 5 and all(item["result"] == "PENDING" for item in items)
        form_evidence = [{"case_revision_id": revision_id, "rule_version_id": state["rule_version_id"]}]
        call(staff, "PATCH", staff_path + f"/review-items/{items[0]['id']}", tag=items[0]["etag"], body={
            "result": "QUESTION", "public_reason": "合成測試：請補付款說明", "evidence_refs": form_evidence,
        })
        assert call(staff, "GET", staff_path).json()["data"]["tasks"] == []
        task = call(staff, "POST", staff_path + "/tasks", key=str(uuid4()), expected=201, body={
            "title": "合成付款證明補件", "requirement": "請補新的合成付款圖片",
            "acceptance_criteria": "由測試承辦核對文件版本與說明",
            "due_at": (utcnow() + timedelta(days=2)).isoformat(),
        }).json()["data"]
        task_path = f"/api/v1/tasks/{task['id']}"
        staff_task = f"/api/v1/staff/tasks/{task['id']}"
        assert call(youth, "GET", task_path).json()["data"]["status"] == "OPEN"
        revised = upload(youth, case_id, "PAYMENT_PROOF", task_id=task["id"], revised=True)
        supplemented = call(youth, "POST", task_path + "/submissions", tag=task["etag"],
                            key=str(uuid4()), expected=201, body={
                                "task_revision": task["task_revision"],
                                "file_version_ids": [revised["file_version_id"]], "statement": "合成補件說明",
                            })
        assert supplemented.json()["data"]["task_status"] == "SUBMITTED"
        acceptance = {"submission_id": supplemented.json()["data"]["receipt"]["submission_id"],
                      "review_note": "合成測試：已對照補件圖片與說明"}
        early = call(staff, "POST", staff_task + "/accept", body=acceptance,
                     tag=supplemented.headers["etag"], expected=409)
        assert early.json()["error"]["code"] == "FILE_NOT_CLEAN"
        assert process_scans(factory, settings) == 7
        for item in [*uploads, revised]:
            metadata = call(youth, "GET", f"/api/v1/files/{item['file_id']}").json()["data"]
            assert metadata["scan_status"] == "CLEAN"
            assert metadata["scan_engine"] == "development-format-only-NOT-ANTIVIRUS"
        assert call(staff, "GET", staff_task).json()["data"]["status"] == "SUBMITTED"
        accepted = call(staff, "POST", staff_task + "/accept", body=acceptance, tag=supplemented.headers["etag"])
        assert accepted.json()["data"]["status"] == "ACCEPTED"
        downloaded = call(staff, "GET", f"/api/v1/files/{revised['file_id']}/download")
        assert downloaded.content == Path(state["documents"]["PAYMENT_PROOF_REVISED"]["path"]).read_bytes()
        evidence = [*form_evidence, {"file_version_id": revised["file_version_id"],
                                    "rule_version_id": state["rule_version_id"]}]
        decision_body = {"outcome": "APPROVED", "reason": "隔離合成流程測試，非真實補助核定",
                         "rule_version_id": state["rule_version_id"], "evidence_refs": evidence}
        current = call(staff, "GET", staff_path)
        blocked = call(staff, "POST", staff_path + "/decisions", body=decision_body,
                       tag=current.headers["etag"], key=str(uuid4()), expected=409)
        assert blocked.json()["error"]["code"] == "REVIEW_INCOMPLETE"
        items = call(staff, "GET", staff_path + "/review-items").json()["data"]["items"]
        for item in items:
            # The test actor supplies every conclusion/evidence; the API does not auto-pass criteria.
            call(staff, "PATCH", staff_path + f"/review-items/{item['id']}", tag=item["etag"], body={
                "result": "PASS", "internal_note": "測試角色明確填入結論與依據，僅驗證 API 流程",
                "evidence_refs": evidence,
            })
        current = call(staff, "GET", staff_path)
        assert current.json()["data"]["status"] == "UNDER_REVIEW"
        decided = call(staff, "POST", staff_path + "/decisions", body=decision_body,
                       tag=current.headers["etag"], key=str(uuid4()), expected=201).json()["data"]
        assert decided["case_status"] == "DECIDED" and decided["decision"]["outcome"] == "APPROVED"
        final = call(youth, "GET", case_path).json()["data"]
        assert final["status"] == "DECIDED" and final["decision"]["outcome"] == "APPROVED"
        assert final["tasks"][0]["status"] == "ACCEPTED"
    print("PASS isolated HTTP workflow: email OTP; six real image uploads; staff MFA; supplement; "
          "scan/accept boundary; five evidence-backed checks; explicit decision. No external delivery.")


def smoke(args):
    with fixture(args.port) as (state, app, settings, factory):
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port,
                                               access_log=False, log_level="critical"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                if not thread.is_alive():
                    raise RuntimeError("The isolated HTTP server stopped before startup.")
                time.sleep(0.1)
            else:
                raise RuntimeError("The isolated HTTP server did not become ready.")
            api_workflow(state, settings, factory, require_web=args.require_web)
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            if thread.is_alive():
                raise RuntimeError("The isolated HTTP server did not stop cleanly.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    server = sub.add_parser("serve", help="Run a disposable browser fixture; delete its data when stopped.")
    server.add_argument("--port", type=int, default=8011)
    server.add_argument("--state-file", type=Path, required=True)
    server.add_argument("--scan-interval", type=float, default=1.0,
                        help="Seconds between development-only scan rounds; 0 disables automatic scans.")
    for action in ("codes", "scan"):
        command = sub.add_parser(action)
        command.add_argument("--state-file", type=Path, required=True)
    check = sub.add_parser("smoke", help="Start a separate temporary API, test the workflow, then delete it.")
    check.add_argument("--port", type=int, default=8012)
    check.add_argument("--require-web", action="store_true", help="Also require both frontend build entries.")
    args = parser.parse_args()
    if args.command == "serve":
        serve(args)
    elif args.command == "smoke":
        smoke(args)
    else:
        state, settings = load_state(args.state_file)
        if args.command == "codes":
            codes(state, settings)
            print("Fresh test codes written to the private codes file.")
        else:
            print(f"Development format scan processed {scan(settings)} file(s); no notifications sent.")


if __name__ == "__main__":
    main()
