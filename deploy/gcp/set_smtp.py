"""Prompt an operator for SMTP delivery settings and store them privately.

The password is read with getpass, so it is never echoed, never placed in argv
and never written to shell history. Nothing is printed back except key names.
Run on the VM as root after init_env.py.
"""

import getpass
import os
import re
import smtplib
import ssl
import subprocess
import sys


MERGE = "/usr/local/sbin/youth-merge-env.py"
ADDRESS = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def ask(label, default=""):
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def main():
    if os.geteuid() != 0:
        print("Run with sudo.", file=sys.stderr)
        return 2
    print("SMTP delivery settings. The password is not echoed and is not logged.\n")
    host = ask("SMTP host", "smtp.gmail.com")
    port = ask("SMTP port", "587")
    username = ask("SMTP username (full Gmail address)")
    if not ADDRESS.fullmatch(username):
        print("That does not look like an email address.", file=sys.stderr)
        return 2
    sender = ask("Sender address shown to citizens", username)
    if not ADDRESS.fullmatch(sender):
        print("That does not look like an email address.", file=sys.stderr)
        return 2
    password = getpass.getpass("App password (input hidden): ")
    # Google shows app passwords in four groups; operators paste them either way.
    password = password.replace(" ", "")
    if len(password) < 8:
        print("That password looks too short.", file=sys.stderr)
        return 2

    print("\nTesting the connection before saving...")
    try:
        with smtplib.SMTP(host, int(port), timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(username, password)
    except (smtplib.SMTPException, OSError, ValueError) as exc:
        # Show the class of failure, never the credential.
        print(f"Login failed ({type(exc).__name__}). Nothing was saved.", file=sys.stderr)
        print("For Gmail this usually means 2-Step Verification is off, or the "
              "16-character app password is wrong.", file=sys.stderr)
        return 1
    print("SMTP login succeeded.")

    payload = "\n".join([
        f"YOUTH_SMTP_HOST={host}",
        f"YOUTH_SMTP_PORT={port}",
        f"YOUTH_SMTP_USERNAME={username}",
        f"YOUTH_SMTP_PASSWORD={password}",
        f"YOUTH_MAIL_FROM={sender}",
        "YOUTH_MAIL_BACKEND=smtp",
        "YOUTH_SMTP_STARTTLS=true",
        "YOUTH_SMTP_USE_TLS=false",
    ]) + "\n"
    result = subprocess.run([sys.executable, MERGE], input=payload, text=True)
    if result.returncode != 0:
        return result.returncode
    print("\nSaved. Activate the release to apply it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
