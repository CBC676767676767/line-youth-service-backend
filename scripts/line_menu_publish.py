"""Publish the validated six-area menu to the real LINE channel.

This is the only script here that reaches the network, and it changes what every
follower sees. It refuses to run without --apply and reads the channel access
token from the environment, never from argv, so it stays out of process lists
and shell history. Buttons only respond once a reachable webhook is configured.
"""

import argparse
import json
import os
import sys

import httpx

from line_menu import MenuValidationError, validate


API = "https://api.line.me/v2/bot"
DATA_API = "https://api-data.line.me/v2/bot"
TOKEN_VAR = "YOUTH_LINE_CHANNEL_ACCESS_TOKEN"
TIMEOUT = 30


class PublishError(Exception):
    """Operator-facing failure that never echoes the token or response secrets."""


def _client(token):
    return httpx.Client(timeout=TIMEOUT, follow_redirects=False, trust_env=False,
                        headers={"Authorization": f"Bearer {token}"})


def _fail(step, response):
    # LINE returns a JSON message; show it, but never the request headers.
    try:
        detail = response.json().get("message", "")
    except ValueError:
        detail = ""
    raise PublishError(f"{step} failed with HTTP {response.status_code}. {detail}".strip())


def publish(token, payload, image, apply_changes):
    with _client(token) as client:
        identity = client.get(f"{API}/info")
        if identity.status_code != 200:
            _fail("Channel lookup", identity)
        bot = identity.json()
        existing = client.get(f"{API}/richmenu/list")
        if existing.status_code != 200:
            _fail("Rich menu listing", existing)
        known = existing.json().get("richmenus", [])
        if not apply_changes:
            return {"status": "dry-run", "bot": bot.get("displayName"),
                    "basicId": bot.get("basicId"), "apiRichMenus": len(known),
                    "wouldCreate": payload["name"], "wouldSetDefault": True,
                    "note": "Re-run with --apply to change what followers see."}

        created = client.post(f"{API}/richmenu", json=payload)
        if created.status_code != 200:
            _fail("Rich menu creation", created)
        menu_id = created.json()["richMenuId"]
        try:
            # The image endpoint lives on the separate api-data host.
            upload = client.post(f"{DATA_API}/richmenu/{menu_id}/content", content=image,
                                 headers={"Content-Type": "image/png"})
            if upload.status_code != 200:
                _fail("Image upload", upload)
            default = client.post(f"{API}/user/all/richmenu/{menu_id}")
            if default.status_code != 200:
                _fail("Default assignment", default)
        except Exception:
            # Never leave an image-less or unassigned menu behind.
            client.delete(f"{API}/richmenu/{menu_id}")
            raise
        return {"status": "published", "richMenuId": menu_id, "bot": bot.get("displayName"),
                "basicId": bot.get("basicId"), "name": payload["name"],
                "previousApiRichMenus": len(known)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Actually create, upload and set the menu as default")
    args = parser.parse_args(argv)
    token = os.environ.get(TOKEN_VAR, "").strip()
    if not token:
        print(json.dumps({"status": "error",
                          "message": f"Set {TOKEN_VAR} in the environment first."}), file=sys.stderr)
        return 2
    try:
        payload, image, digest = validate()
        result = publish(token, payload, image, args.apply)
    except (MenuValidationError, PublishError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except httpx.HTTPError:
        print(json.dumps({"status": "error", "message": "Could not reach the LINE API."}),
              file=sys.stderr)
        return 1
    result["contentHash"] = digest
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
