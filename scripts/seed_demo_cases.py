"""Create synthetic applications against a running demo deployment.

Every field is obviously fake and every uploaded file is a generated placeholder
image, so nothing here resembles a real applicant. It logs in with the fixed demo
one-time code, so a deployment that issues real random codes rejects it at the
verify step and no case is created.

    python scripts/seed_demo_cases.py --base-url https://host --count 4

It drives the same public API a citizen uses: email code, draft, six documents,
antivirus scan, then submit. It never touches staff review or decisions.
"""

import argparse
import json
import struct
import sys
import time
import uuid
import zlib

import httpx


SCHEME = "hsinchu-ai-grant-2026"
DOCUMENTS = ["ID_FRONT", "ID_BACK", "RECEIPT", "PAYMENT_PROOF", "BANK_ACCOUNT", "AFFIDAVIT"]
APPLICANTS = [
    ("示範 甲", "ChatGPT Plus", "2026-09-01", "600", "300", "monthly", "official"),
    ("示範 乙", "Claude Pro", "2026-08-20", "700", "350", "monthly", "official"),
    ("示範 丙", "Midjourney 標準版", "2026-08-05", "960", "480", "monthly", "reseller"),
    ("示範 丁", "Canva AI 年繳", "2026-07-18", "3600", "1800", "annual", "official"),
    ("示範 戊", "Perplexity Pro", "2026-09-10", "600", "300", "monthly", "official"),
    ("示範 己", "Figma AI", "2026-06-30", "540", "270", "monthly", "official"),
]


def placeholder_png(label: str) -> bytes:
    """A tiny valid PNG. Storage checks magic bytes, so a stub file will not do."""
    width = height = 16
    shade = (sum(label.encode()) % 160) + 60
    raw = b"".join(b"\x00" + bytes([shade, shade, shade]) * width for _ in range(height))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class SeedError(Exception):
    """Operator-facing failure; the response body is shown, never a credential."""


def detail(response: httpx.Response) -> str:
    try:
        payload = response.json().get("error", {})
        errors = "；".join(item.get("message", "") for item in payload.get("field_errors", []))
        return f"{payload.get('code', response.status_code)} {payload.get('message', '')} {errors}".strip()
    except ValueError:
        return f"HTTP {response.status_code}"


def expect(response: httpx.Response, *allowed: int) -> dict:
    if response.status_code not in allowed:
        raise SeedError(detail(response))
    return response.json().get("data", {})


def upload(client: httpx.Client, case_id: str, document_type: str,
           content: bytes | None = None) -> tuple[str, str]:
    content = placeholder_png(document_type) if content is None else content
    intent = expect(client.post("/files/upload-intents", json={
        "case_id": case_id, "task_id": None, "document_type": document_type,
        "file_name": f"demo-{document_type.lower()}.png",
        "size_bytes": len(content), "content_type": "image/png",
    }), 200, 201)
    put = client.put(intent["upload_url"].removeprefix("/api/v1"), content=content,
                     headers=intent["upload_headers"])
    expect(put, 200, 201, 204)
    done = expect(client.post(f"/files/{intent['file_id']}/complete",
                              json={"file_version_id": intent["file_version_id"]}), 200, 201)
    return intent["file_id"], done["file_version_id"]


def wait_for_clean(client: httpx.Client, file_ids: list[str], timeout: int = 240) -> None:
    """Submission only accepts CLEAN versions; the maintenance worker scans them."""
    deadline = time.monotonic() + timeout
    pending = list(file_ids)
    while pending and time.monotonic() < deadline:
        still = []
        for file_id in pending:
            state = expect(client.get(f"/files/{file_id}"), 200).get("scan_status")
            if state == "CLEAN":
                continue
            if state in {"REJECTED", "SCAN_FAILED", "EXPIRED"}:
                raise SeedError(f"antivirus returned {state} for a placeholder file")
            still.append(file_id)
        pending = still
        if pending:
            time.sleep(3)
    if pending:
        raise SeedError("timed out waiting for the antivirus scan")


def seed_one(base_url: str, index: int, tag: str) -> str:
    name, tool, purchased, amount, requested, plan, channel = APPLICANTS[index % len(APPLICANTS)]
    email = f"demo.{tag}{index + 1}@example.com"
    with httpx.Client(base_url=base_url.rstrip("/") + "/api/v1", timeout=60,
                      headers={"Origin": base_url.rstrip("/")}, follow_redirects=False) as client:
        challenge = expect(client.post("/auth/email/challenges", json={"email": email}), 200, 202)
        session = expect(client.post("/auth/email/challenges/verify", json={
            "challenge_id": challenge["challenge_id"], "code": "698217"}), 200)
        client.headers["X-CSRF-Token"] = session["csrf_token"]

        case = expect(client.post("/cases", json={"scheme_id": SCHEME},
                                  headers={"Idempotency-Key": str(uuid.uuid4())}), 200, 201)
        form = {"name": name, "email": email, "birth": "2001-05-16", "city": "新竹市",
                "tool": tool, "purchaseDate": purchased, "amount": amount,
                "requested": requested, "channel": channel, "plan": plan,
                "payer": "self", "special": False, "paymentMethod": "card"}
        case = expect(client.patch(f"/cases/{case['id']}", json={"form_data": form},
                                   headers={"If-Match": case["etag"]}), 200)

        uploaded = [upload(client, case["id"], document) for document in DOCUMENTS]
        wait_for_clean(client, [file_id for file_id, _ in uploaded])
        versions = [version for _, version in uploaded]
        case = expect(client.get(f"/cases/{case['id']}"), 200)
        submitted = expect(client.post(f"/cases/{case['id']}/submit",
                                       json={"file_version_ids": versions},
                                       headers={"If-Match": case["etag"],
                                                "Idempotency-Key": str(uuid.uuid4())}), 200, 201)
        return f"{submitted.get('case_no', case['case_no'])} → {submitted.get('status', '?')}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--tag", default="a",
                        help="Mailbox prefix; change it when the send throttle rejects a rerun")
    args = parser.parse_args(argv)
    if not args.base_url.startswith("https://"):
        print(json.dumps({"status": "error", "message": "Use the https deployment URL."}),
              file=sys.stderr)
        return 2
    created, failed = [], []
    for index in range(max(1, args.count)):
        try:
            created.append(seed_one(args.base_url, index, args.tag))
            print(f"created {created[-1]}", flush=True)
        except (SeedError, httpx.HTTPError) as exc:
            failed.append(f"#{index + 1}: {exc}")
            print(f"failed  #{index + 1}: {exc}", file=sys.stderr, flush=True)
    print(json.dumps({"created": len(created), "failed": failed}, ensure_ascii=False))
    return 1 if failed and not created else 0


if __name__ == "__main__":
    raise SystemExit(main())
