"""Local operator commands. These are not exposed through HTTP."""
import argparse
import json
import os
from pathlib import Path
import secrets
import time

from cryptography.fernet import Fernet


def main():
    parser = argparse.ArgumentParser(description="青年案件後端操作工具")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create a private .env; never overwrite an existing one")
    commands.add_parser("seed", help="Seed the demonstration scheme")
    staff = commands.add_parser("create-staff")
    staff.add_argument("--email", required=True)
    staff.add_argument("--role", required=True, choices=["reviewer", "supervisor", "admin", "auditor"])
    worker = commands.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    commands.add_parser("show-mail", help="Read local development mail; disabled in production")
    args = parser.parse_args()
    if args.command == "init":
        password = secrets.token_urlsafe(24)
        content = (f"POSTGRES_PASSWORD={password}\n"
                   f"YOUTH_DATABASE_URL=postgresql+psycopg://youth:{password}@127.0.0.1:55432/youth\n"
                   "YOUTH_APP_ENV=development\n"
                   f"YOUTH_SECRET_KEY={secrets.token_urlsafe(48)}\n"
                   f"YOUTH_TOTP_ENCRYPTION_KEY={Fernet.generate_key().decode()}\n")
        try:
            fd = os.open(".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            parser.exit(1, ".env already exists; nothing overwritten.\n")
        with os.fdopen(fd, "w") as output:
            output.write(content)
        print("Created private .env (0600). Run docker compose up -d db, then alembic upgrade head.")
        return

    from app.config import Settings
    from app.db import make_engine, make_session_factory
    settings = Settings()
    if args.command == "show-mail":
        if settings.app_env == "production" or settings.mail_backend != "spool":
            parser.exit(1, "Local mail inspection is development-only.\n")
        from email import policy
        from email.parser import BytesParser
        for path in sorted(Path(settings.mail_spool_dir).glob("*.eml"), key=lambda p: p.stat().st_mtime)[-5:]:
            message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
            print(f"To: {message['To']}\n{message.get_body(preferencelist=('plain',)).get_content()}")
        return
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    if args.command == "worker":
        from app.worker import run_once
        while True:
            result = run_once(factory, settings)
            if args.once:
                print(json.dumps(result, default=str))
                break
            time.sleep(settings.worker_poll_seconds)
        return
    from app.bootstrap import create_staff, seed_scheme
    with factory() as db:
        seed_scheme(db)
        if args.command == "create-staff":
            credentials = create_staff(db, settings, args.email, args.role)
            db.commit()
            print(json.dumps(credentials, ensure_ascii=False, indent=2))
            print("Record these one-time enrollment credentials privately; the API never returns them.")
        else:
            db.commit()
            print("Demonstration scheme youth-demo is ready.")
    engine.dispose()


if __name__ == "__main__":
    main()
