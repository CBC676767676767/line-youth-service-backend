"""Prepare one demo mailbox with a ready application: form, six documents, submitted.

    python scripts/seed_demo_account.py --base-url https://host

Sign in afterwards with the mailbox it prints and the fixed demo code. Nothing
here is a shortcut past the product: it drives the same public API a citizen
does, so a deployment issuing real random codes rejects it at the verify step
and no case is created.

The documents are drawn by scripts/demo_documents.py. They are invented, and
deliberately so -- a real receipt carries the buyer's own mailbox and postal
address, which must never be seeded into a deployment that other people see.

Pass --stage draft to stop before submitting, when the demo is the applicant
filling the form rather than the caseworker reviewing it.
"""

import argparse
import io
import json
import sys
import uuid
from pathlib import Path

import httpx
from PIL import Image

from scripts.demo_documents import FontMissing, build
from scripts.seed_demo_cases import (
    DOCUMENTS,
    SCHEME,
    SeedError,
    expect,
    upload,
    wait_for_clean,
)

EMAIL = "demo.showcase@example.com"
FORM = {
    "name": "林小竹",
    "email": EMAIL,
    "birth": "2001-05-16",
    "city": "新竹市",
    "tool": "Claude Pro",
    "purchaseDate": "2026-09-07",
    "amount": "650",
    "requested": "325",
    "channel": "official",
    "plan": "monthly",
    "payer": "self",
    "special": False,
    "paymentMethod": "card",
}


MIN_EDGE = 1600


def supplied_image(path: Path) -> bytes:
    """Read an operator-supplied card scan and bring it up to a readable size.

    Card photographs are routinely a few hundred pixels wide, which leaves a
    Chinese glyph around 12 px tall -- too small for the citizen page's
    recognition and too small to read in the staff viewer. Enlarging adds no
    information, it only stops the detail that is there from being thrown away.
    The file is never taken on trust: it is decoded and re-encoded as PNG, which
    drops any metadata and rejects anything that is not actually an image.
    """
    with Image.open(path) as image:
        image = image.convert("RGB")
        longest = max(image.size)
        if longest < MIN_EDGE:
            scale = MIN_EDGE / longest
            image = image.resize(
                (round(image.width * scale), round(image.height * scale)),
                Image.LANCZOS,
            )
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()


def seed(base_url: str, email: str, code: str, stage: str,
         supplied: dict[str, Path]) -> dict:
    root = base_url.rstrip("/")
    with httpx.Client(base_url=root + "/api/v1", timeout=90,
                      headers={"Origin": root}, follow_redirects=False) as client:
        challenge = expect(client.post("/auth/email/challenges", json={"email": email}), 200, 202)
        session = expect(client.post("/auth/email/challenges/verify", json={
            "challenge_id": challenge["challenge_id"], "code": code}), 200)
        client.headers["X-CSRF-Token"] = session["csrf_token"]

        case = expect(client.post("/cases", json={"scheme_id": SCHEME},
                                  headers={"Idempotency-Key": str(uuid.uuid4())}), 200, 201)
        case = expect(client.patch(f"/cases/{case['id']}", json={"form_data": dict(FORM, email=email)},
                                   headers={"If-Match": case["etag"]}), 200)

        uploaded = [
            upload(client, case["id"], document,
                   supplied_image(supplied[document]) if document in supplied else build(document))
            for document in DOCUMENTS
        ]
        wait_for_clean(client, [file_id for file_id, _ in uploaded])
        if stage == "draft":
            case = expect(client.get(f"/cases/{case['id']}"), 200)
            return {"email": email, "case_no": case["case_no"], "status": case["status"],
                    "documents": len(uploaded)}

        case = expect(client.get(f"/cases/{case['id']}"), 200)
        submitted = expect(client.post(f"/cases/{case['id']}/submit",
                                       json={"file_version_ids": [v for _, v in uploaded]},
                                       headers={"If-Match": case["etag"],
                                                "Idempotency-Key": str(uuid.uuid4())}), 200, 201)
        return {"email": email, "case_no": submitted.get("case_no", case["case_no"]),
                "status": submitted.get("status", "?"), "documents": len(uploaded)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--email", default=EMAIL,
                        help="Change it when the send throttle rejects a rerun")
    parser.add_argument("--code", default="698217", help="The deployment's fixed demo code")
    parser.add_argument("--stage", choices=("draft", "submitted"), default="submitted")
    parser.add_argument("--name", default=FORM["name"],
                        help="Applicant name; set it to whatever the supplied card shows")
    for document in DOCUMENTS:
        parser.add_argument(f"--{document.lower().replace('_', '-')}", type=Path,
                            help=f"Use this image for {document} instead of a drawn one")
    args = parser.parse_args(argv)
    if not args.base_url.startswith("https://"):
        print(json.dumps({"status": "error", "message": "Use the https deployment URL."}),
              file=sys.stderr)
        return 2
    supplied = {}
    for document in DOCUMENTS:
        path = getattr(args, document.lower())
        if path is None:
            continue
        if not path.is_file():
            print(json.dumps({"status": "error", "message": f"找不到檔案：{path}"},
                             ensure_ascii=False), file=sys.stderr)
            return 2
        supplied[document] = path
    FORM["name"] = args.name
    try:
        result = seed(args.base_url, args.email, args.code, args.stage, supplied)
    except FontMissing as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 3
    except (SeedError, httpx.HTTPError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
