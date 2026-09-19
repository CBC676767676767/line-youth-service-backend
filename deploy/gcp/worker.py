"""Deployment worker: file processing by default; outbound case messages opt in."""
import argparse
import os
import signal
import threading


def tick(mode, factory, settings):
    if mode == "notifications":
        from app.worker import process_notifications
        return {"notifications": process_notifications(factory, settings)}
    from app.admin import process_exports
    from app.worker import process_inbox, process_scans
    return {"scans": process_scans(factory, settings),
            "inbox": process_inbox(factory, settings),
            "exports": process_exports(factory, settings)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["maintenance", "notifications"])
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.mode == "notifications" and os.getenv("YOUTH_ENABLE_CASE_NOTIFICATIONS") != "true":
        parser.exit(2, "Outbound case notifications are not enabled.\n")
    from app.config import Settings
    from app.db import make_engine, make_session_factory
    try:
        settings = Settings()
    except Exception:
        parser.exit(2, "Invalid runtime settings; details withheld to protect secrets.\n")
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    try:
        while not stop.is_set():
            try:
                tick(args.mode, factory, settings)
            except Exception:
                # No exception body, document details, recipients or tokens in logs.
                print("Worker pass failed; inspect service health and private diagnostics.", flush=True)
                if args.once:
                    raise SystemExit(1) from None
            if args.once:
                break
            stop.wait(settings.worker_poll_seconds)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
