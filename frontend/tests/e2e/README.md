# Isolated browser verification

These tests exercise the real built citizen portal, precheck wizard and staff portal through a fresh headless browser. They refuse to operate unless `scripts/web_smoke.py serve` identifies itself as the exact disposable fixture in the private state file. The fixture uses synthetic accounts/documents, private email spool, SQLite and the development format scanner; it disables SMTP, LINE and government integrations.

First build the frontend (`npm run build`) and start the disposable fixture as described in `scripts/web_smoke.py --help`. Keep its state file outside the repository with mode 0600. Do not copy that file, generated codes or authenticated browser state into test artifacts.

Set `YOUTH_E2E_STATE_FILE` to the fixture's state path and `YOUTH_E2E_PYTHON` to the Python interpreter used to run it. `YOUTH_E2E_SMOKE_SCRIPT` can override the absolute script path. On Windows with a WSL fixture, these three paths must be Linux paths; `YOUTH_E2E_WSL_DISTRO` defaults to `kali-linux`. The test runner reads only the private fixture and synthetic document paths through the WSL filesystem.

Run `npm run test:e2e`. On Windows the runner uses installed Microsoft Edge with an isolated temporary profile. Else it uses installed Playwright Chromium, or the executable specified by `YOUTH_E2E_BROWSER`. This command is independent of the existing `npm test` suite.

The real-HTTP workflow covers anonymous precheck, email OTP, server snapshot persistence, citizen snapshot display, opt-in import with conflict preservation and no background write, unsaved navigation/refresh guards, grant draft save/reload, six PNG uploads, a deliberately lost submission response followed by the same idempotency key, staff password and TOTP login, review start, supplement creation/upload/submission and acceptance, sibling review edits, five explicit evidence-backed test reviews and a synthetic supervisor decision. The mobile check covers planning without purchase/payment requirements. No test claims a real document, subsidy eligibility, payment or antivirus verification.

Traces, videos, screenshots and browser storage-state exports are disabled so fixture credentials and codes cannot become report artifacts. Tests retain only assertion results. Stop the fixture when finished to delete its temporary database, documents, mail and credentials.
