"""Offline deployment checks: synthetic files only, no Docker/cloud/network calls."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DeploymentTests(unittest.TestCase):
    def test_source_archive_is_deterministic_and_excludes_runtime_data(self):
        packer = load("create_package")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            for name in packer.FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("source\n", encoding="utf-8")
            for name in packer.TREES:
                (root / name).mkdir(parents=True, exist_ok=True)
            for name in ["app/main.py", "frontend/src/main.tsx", "deploy/gcp/runtime.env.example"]:
                (root / name).write_text("public source\n", encoding="utf-8")
            excluded = [".env", "var/database.sqlite", "tests/e2e/fixture-secret.json", ".git/config",
                        "frontend/node_modules/package/index.js", "frontend/src/.env.local",
                        "app/private.key", "app/runtime.env", "app/__pycache__/main.pyc"]
            for name in excluded:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("SYNTHETIC_PRIVATE_MARKER", encoding="utf-8")
            first, second = Path(temp) / "first.tar.gz", Path(temp) / "second.tar.gz"
            digest, count = packer.create_package(root, first)
            os.utime(root / "app/main.py", (1, 1))
            self.assertEqual(packer.create_package(root, second), (digest, count))
            with tarfile.open(first) as archive:
                names = archive.getnames()
                self.assertIn("deploy/gcp/runtime.env.example", names)
                self.assertIn("app/main.py", names)
                for name in excluded:
                    self.assertNotIn(name, names)
                for member in archive:
                    self.assertEqual(member.mtime, 0)
                    self.assertNotIn(b"SYNTHETIC_PRIVATE_MARKER", archive.extractfile(member).read())

    def test_environment_generator_is_private_and_never_overwrites_or_prints_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "runtime.env"
            command = [sys.executable, str(ROOT / "init_env.py"), "--domain", "service.example.test",
                       "--output", str(output)]
            first = subprocess.run(command, capture_output=True, text=True, check=True)
            contents = output.read_text(encoding="utf-8")
            self.assertIn("YOUTH_APP_ENV=production", contents)
            self.assertIn("YOUTH_LINE_REPLY_MODE=disabled", contents)
            self.assertIn("YOUTH_LINE_SIMULATOR_ENABLED=false", contents)
            secret = next(line.partition("=")[2] for line in contents.splitlines()
                          if line.startswith("YOUTH_SECRET_KEY="))
            self.assertGreaterEqual(len(secret), 32)
            self.assertNotIn(secret, first.stdout + first.stderr)
            if os.name == "posix":
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            second = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(output.read_text(encoding="utf-8"), contents)

    def test_default_worker_does_not_call_outbound_notifications(self):
        worker = load("worker")
        calls = []
        fake_worker = types.ModuleType("app.worker")
        fake_admin = types.ModuleType("app.admin")
        for name in ("process_scans", "process_inbox", "process_notifications"):
            setattr(fake_worker, name, lambda *_, label=name: calls.append(label) or 0)
        fake_admin.process_exports = lambda *_: calls.append("process_exports") or 0
        with patch.dict(sys.modules, {"app.worker": fake_worker, "app.admin": fake_admin}):
            worker.tick("maintenance", object(), object())
            self.assertEqual(calls, ["process_scans", "process_inbox", "process_exports"])
            calls.clear()
            worker.tick("notifications", object(), object())
            self.assertEqual(calls, ["process_notifications"])

    def test_notifications_require_explicit_enable_before_loading_app_settings(self):
        env = {key: value for key, value in os.environ.items() if key != "YOUTH_ENABLE_CASE_NOTIFICATIONS"}
        result = subprocess.run([sys.executable, str(ROOT / "worker.py"), "notifications", "--once"],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not enabled", result.stderr)


if __name__ == "__main__":
    unittest.main()
