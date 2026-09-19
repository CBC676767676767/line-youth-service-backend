"""Point the LINE channel at this deployment's webhook and test it for real.

The path is read from deploy/gcp/vm-plan.json so it cannot drift from the route
the application registers. `test` asks LINE to deliver a real signed request and
reports what LINE saw; a 200 here means LINE reached the endpoint and the
signature verified, not that any case flow works.

Set YOUTH_LINE_CHANNEL_ACCESS_TOKEN in the environment; it is never read from argv.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "deploy/gcp/vm-plan.json"
API = "https://api.line.me/v2/bot/channel/webhook"
TOKEN_VAR = "YOUTH_LINE_CHANNEL_ACCESS_TOKEN"


class WebhookError(Exception):
    """Operator-facing failure that never echoes the access token."""


def webhook_path() -> str:
    path = json.loads(PLAN.read_text(encoding="utf-8"))["public_webhook_path"]
    if not path.startswith("/") or "//" in path or ".." in path:
        raise WebhookError("vm-plan.json holds an unusable webhook path.")
    return path


def endpoint_for(domain: str) -> str:
    # Accept a bare hostname or a full URL, but always publish https and our path.
    host = domain if "://" in domain else f"https://{domain}"
    parts = urlsplit(host)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise WebhookError("The domain must resolve to a plain https host.")
    return f"https://{parts.netloc}{webhook_path()}"


def call(client, method, suffix="", payload=None):
    response = client.request(method, API + suffix, json=payload)
    if response.status_code != 200:
        try:
            detail = response.json().get("message", "")
        except ValueError:
            detail = ""
        raise WebhookError(f"LINE returned HTTP {response.status_code}. {detail}".strip())
    return response.json() if response.content else {}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show", help="Report the endpoint LINE currently has")
    setter = commands.add_parser("set", help="Point LINE at this deployment")
    setter.add_argument("--domain", required=True, help="Public hostname, e.g. 203.0.113.9.sslip.io")
    commands.add_parser("test", help="Ask LINE to deliver a real signed request")
    args = parser.parse_args(argv)

    token = os.environ.get(TOKEN_VAR, "").strip()
    if not token:
        print(json.dumps({"status": "error", "message": f"Set {TOKEN_VAR} first."}), file=sys.stderr)
        return 2
    try:
        with httpx.Client(timeout=30, follow_redirects=False, trust_env=False,
                          headers={"Authorization": f"Bearer {token}"}) as client:
            if args.command == "show":
                result = call(client, "GET", "/endpoint")
            elif args.command == "set":
                endpoint = endpoint_for(args.domain)
                call(client, "PUT", "/endpoint", {"endpoint": endpoint})
                result = call(client, "GET", "/endpoint")
                result["requested"] = endpoint
            else:
                # LINE posts a genuine signed event; a non-200 detail is the server's.
                result = call(client, "POST", "/test")
                result["reachable"] = result.get("success") is True and result.get("statusCode") == 200
    except WebhookError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except (httpx.HTTPError, OSError, ValueError):
        print(json.dumps({"status": "error", "message": "Could not reach the LINE API."}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
