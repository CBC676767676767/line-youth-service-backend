"""Run a local, persistent synthetic-data demo using the existing application.

Linux/WSL is required by the existing private-mail and attachment adapters.
No worker or external delivery is started. Existing demo data is never reset.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


LINE_PROFILE_KEYS = {
    "YOUTH_LINE_CHANNEL_ID", "YOUTH_LINE_PROVIDER_ID", "YOUTH_LINE_MESSAGING_CHANNEL_ID",
    "YOUTH_LINE_CHANNEL_SECRET", "YOUTH_LINE_CHANNEL_ACCESS_TOKEN", "YOUTH_LINE_DESTINATION_USER_ID",
}


def load_line_profile(path: Path) -> dict[str, str]:
    """Read an explicitly selected, private local profile; never print its values."""
    if path.is_symlink():
        raise ValueError("LINE profile must not be a symlink")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("LINE profile must be a current-user private file (0600)")
        if info.st_size > 16384:
            raise ValueError("LINE profile is too large")
        values = json.load(stream)
    if (not isinstance(values, dict) or set(values) - LINE_PROFILE_KEYS
            or any(not isinstance(value, str) or len(value) > 4096 or "\n" in value or "\r" in value
                   for value in values.values())):
        raise ValueError("LINE profile contains unsupported settings")
    required = LINE_PROFILE_KEYS - {"YOUTH_LINE_CHANNEL_ID", "YOUTH_LINE_PROVIDER_ID"}
    if any(not values.get(key) for key in required):
        raise ValueError("LINE Messaging profile is incomplete")
    return values


def main():
    parser = argparse.ArgumentParser(description="青年補助預檢本機示範（非正式申辦）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--policy", choices=["demo", "hsinchu"], default="hsinchu")
    parser.add_argument("--line-profile", type=Path, help="Private LINE JSON profile; never starts a sender")
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("Existing private storage requires Linux. Run this script in WSL or Docker.")
    if not 1024 <= args.port <= 65535:
        parser.error("Choose a port between 1024 and 65535.")
    repository = Path(__file__).resolve().parents[1]
    identity = hashlib.sha256(str(repository).encode()).hexdigest()[:12]
    runtime = Path(f"/var/tmp/youth-precheck-{os.getuid()}-{identity}")
    if runtime.is_symlink():
        parser.error("Refusing a symlink runtime directory.")
    runtime.mkdir(mode=0o700, exist_ok=True)
    info = runtime.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        parser.error("Runtime directory must be owned by the current Linux user.")
    runtime.chmod(0o700)
    origin = f"http://127.0.0.1:{args.port}"
    environment = os.environ.copy()
    policy_file = "precheck-demo.json" if args.policy == "demo" else "precheck-hsinchu-115.json"
    environment.update({
        "YOUTH_APP_ENV": "development", "YOUTH_DATABASE_URL": f"sqlite:///{runtime / 'youth.db'}",
        "YOUTH_PUBLIC_ORIGIN": origin, "YOUTH_ALLOWED_ORIGINS": json.dumps([origin]),
        "YOUTH_SECRETS_FILE": str(runtime / "secrets.json"),
        "YOUTH_SECRET_KEY": "", "YOUTH_TOTP_ENCRYPTION_KEY": "",
        "YOUTH_MAIL_SPOOL_DIR": str(runtime / "mail"), "YOUTH_STORAGE_DIR": str(runtime / "files"),
        "YOUTH_MAIL_BACKEND": "spool", "YOUTH_SCAN_BACKEND": "development",
        "YOUTH_LINE_CHANNEL_ACCESS_TOKEN": "", "YOUTH_AUTO_CREATE_SCHEMA": "false",
        "YOUTH_LINE_BOT_ENABLED": "false", "YOUTH_LINE_REPLY_MODE": "disabled",
        "YOUTH_LINE_SIMULATOR_ENABLED": "false",
        "YOUTH_COOKIE_SECURE": "false", "YOUTH_PRECHECK_DEMO_ENABLED": "true",
        "YOUTH_PRECHECK_RULES_PATH": str(repository / "app" / "data" / policy_file),
        "YOUTH_PRECHECK_OFFICIAL_APPLICATION_URL":
            "https://dgservice.hccg.gov.tw/serviceNotice.do?id=1323&rule=guest",
    })
    if args.line_profile:
        try:
            environment.update(load_line_profile(args.line_profile))
        except (OSError, ValueError):
            parser.error("Cannot load a complete, private LINE profile; values were not logged.")
    for arguments in [["alembic", "upgrade", "head"], ["app.cli", "seed"], ["app.cli", "seed-grant"]]:
        subprocess.run([sys.executable, "-m", *arguments], cwd=repository, env=environment, check=True)
    print(f"Local precheck: {origin}/precheck\nLocal test data: {runtime}\nPolicy: {args.policy}", flush=True)
    print("Development OTP: read the newest .eml file in the runtime mail directory.", flush=True)
    print("This is not a government submission and does not reserve a deadline or subsidy.", flush=True)
    if args.line_profile:
        print("LINE Messaging profile loaded. No notification worker is started.", flush=True)
    subprocess.run([sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory",
                      "--host", "127.0.0.1", "--port", str(args.port), "--no-access-log"],
                   cwd=repository, env=environment, check=True)


if __name__ == "__main__":
    main()
