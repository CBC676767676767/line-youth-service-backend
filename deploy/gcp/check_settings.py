"""Fail closed before exposing a deployment; never print settings or secret values.

Two profiles, both fail closed. `production` is the full bar: real SMTP, real
antivirus, production app_env. `demo` drops only outbound email, and says so out
loud: it requires the spool backend precisely so nothing can claim a message was
sent. Everything that protects the public edge -- HTTPS origin, secure cookies,
explicit migrations, no simulator, real ClamAV -- still applies to both.
"""
import argparse
import os
import re
import sys


PLACEHOLDER_TLDS = (".invalid", ".example", ".test", ".localhost", ".local")


def checked_domain() -> str:
    domain = os.environ.get("YOUTH_DOMAIN", "")
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", domain):
        raise ValueError("domain")
    if domain.endswith(PLACEHOLDER_TLDS):
        raise ValueError("placeholder domain")
    return domain


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=["production", "demo"], default="production")
    args = parser.parse_args(argv)

    from app.config import Settings
    try:
        settings = Settings()
        origin = "https://" + checked_domain()
        # The staff portal runs on its own host and address, so its origin has to
        # be allowed too. It stays an explicit, checked hostname: the allowlist
        # never grows beyond the citizen entry and that one staff entry.
        expected = [origin]
        staff = os.environ.get("YOUTH_ADMIN_DOMAIN", "")
        if staff:
            assert re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", staff)
            assert not staff.endswith(PLACEHOLDER_TLDS) and staff != checked_domain()
            expected.append("https://" + staff)
        # Shared edge requirements: identical for both profiles.
        assert settings.public_origin == origin and settings.allowed_origins == expected
        assert settings.cookie_secure and not settings.auto_create_schema
        assert not settings.line_simulator_enabled
        assert settings.scan_backend == "clamav"
        assert settings.database_url.startswith("postgresql")
        assert settings.line_reply_mode != "fake"
        if args.profile == "production":
            assert settings.app_env == "production"
            assert not settings.precheck_demo_enabled
            assert settings.mail_backend == "smtp"
            assert settings.smtp_host and not settings.smtp_host.endswith(PLACEHOLDER_TLDS)
            assert "@" in settings.mail_from and not settings.mail_from.endswith(PLACEHOLDER_TLDS)
        else:
            # The app's own production guards would reject a spool backend, so a
            # demo must not claim production. Spool is required, not merely
            # allowed: a half-configured SMTP host must not silently drop mail.
            assert settings.app_env == "development"
            assert settings.mail_backend == "spool" and not settings.smtp_host
    except Exception:
        print("Settings incomplete for this profile. Check domain/origins, keys, database, "
              "antivirus, disabled simulator and mail backend.", file=sys.stderr)
        return 2

    if args.profile == "production":
        print("Production settings validated; external delivery and DNS have not been tested.")
        return 0
    print("DEMO settings validated. This is NOT a production deployment:")
    print("  - No email is delivered. Mail goes to a private spool, so the email")
    print("    one-time code login cannot complete for any citizen.")
    print("  - Interactive API docs are reachable inside the container; only the")
    print("    Caddy edge keeps them off the internet.")
    print("  - Do not process real applications or real personal data here.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
