"""Merge KEY=VALUE lines from stdin into the private runtime settings file.

Values arrive on stdin so they never appear in argv, the process list or shell
history. Nothing is echoed back: the script reports key names and lengths only.
Run it as root; the file stays 0600 and root-owned.
"""

import os
import sys


TARGET = "/etc/youth-service/runtime.env"


def main():
    if os.geteuid() != 0:
        print("Run this as root.", file=sys.stderr)
        return 2
    updates = {}
    for raw in sys.stdin:
        line = raw.rstrip("\r\n")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key.replace("_", "").isalnum():
            print(f"Refusing suspicious key name: {key!r}", file=sys.stderr)
            return 2
        updates[key] = value
    if not updates:
        print("No assignments received.", file=sys.stderr)
        return 2

    try:
        existing = open(TARGET, encoding="utf-8").read().splitlines()
    except OSError:
        print(f"Initialize {TARGET} first.", file=sys.stderr)
        return 2

    remaining = dict(updates)
    merged, changed = [], []
    for line in existing:
        key = line.split("=", 1)[0] if "=" in line else None
        if key in remaining:
            merged.append(f"{key}={remaining.pop(key)}")
            changed.append(key)
        else:
            merged.append(line)
    for key, value in remaining.items():
        merged.append(f"{key}={value}")
        changed.append(key)

    # Replace in place through a private temp file so a crash cannot truncate settings.
    temporary = TARGET + ".new"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write("\n".join(merged) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, TARGET)
    os.chmod(TARGET, 0o600)
    os.chown(TARGET, 0, 0)
    for key in changed:
        print(f"set {key} ({len(updates[key])} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
