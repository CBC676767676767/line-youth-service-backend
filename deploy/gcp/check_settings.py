"""Fail closed before exposing production; never print settings or secret values."""
import os
import re
import sys


def main():
    from app.config import Settings
    try:
        settings = Settings()
        domain = os.environ.get("YOUTH_DOMAIN", "")
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", domain):
            raise ValueError("domain")
        if domain.endswith((".invalid", ".example", ".test", ".localhost", ".local")):
            raise ValueError("placeholder domain")
        origin = "https://" + domain
        assert settings.app_env == "production"
        assert settings.public_origin == origin and settings.allowed_origins == [origin]
        assert settings.cookie_secure and not settings.auto_create_schema
        assert not settings.precheck_demo_enabled and not settings.line_simulator_enabled
        assert settings.mail_backend == "smtp" and settings.scan_backend == "clamav"
        assert settings.smtp_host and not settings.smtp_host.endswith((".invalid", ".test", ".example"))
        assert "@" in settings.mail_from and not settings.mail_from.endswith((".invalid", ".test", ".example"))
        assert settings.line_reply_mode != "fake"
    except Exception:
        print("Production settings incomplete. Check domain/origins, SMTP, keys, database and disabled demos.", file=sys.stderr)
        return 2
    print("Production settings validated; external delivery and DNS have not been tested.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
