"""Create private VM settings once; secrets are generated locally and never printed."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import secrets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--output", type=Path, default=Path("/etc/youth-service/runtime.env"))
    args = parser.parse_args()
    domain = args.domain.lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}", domain):
        parser.error("Provide a DNS hostname without a URL, path or port")
    origin = "https://" + domain
    password = secrets.token_hex(24)
    replacements = {
        "YOUTH_DOMAIN": domain,
        "YOUTH_PUBLIC_ORIGIN": origin,
        "YOUTH_ALLOWED_ORIGINS": "'" + json.dumps([origin], separators=(",", ":")) + "'",
        "POSTGRES_PASSWORD": password,
        "YOUTH_DATABASE_URL": f"postgresql+psycopg://youth:{password}@db:5432/youth",
        "YOUTH_SECRET_KEY": secrets.token_urlsafe(48),
        "YOUTH_TOTP_ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        "YOUTH_LINE_PUBLIC_PRECHECK_URL": origin + "/precheck",
    }
    template = Path(__file__).with_name("runtime.env.example").read_text(encoding="utf-8")
    lines = []
    for line in template.splitlines():
        name = line.partition("=")[0]
        lines.append(name + "=" + replacements[name] if name in replacements else line)
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        parser.exit(1, "Settings already exist; nothing was overwritten.\n")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(lines) + "\n")
    print("Private settings created. Complete SMTP and optional LINE configuration with sudoedit before activation.")


if __name__ == "__main__":
    main()
