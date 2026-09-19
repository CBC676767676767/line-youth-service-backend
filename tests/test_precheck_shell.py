"""HTTP privacy and UI integration boundaries for the precheck application."""

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def shell_client(tmp_path):
    settings = Settings(
        app_env="test", _env_file=None, database_url=f"sqlite:///{tmp_path / 'shell.db'}",
        secret_key="precheck-shell-test-" * 4,
        totp_encryption_key=Fernet.generate_key().decode(),
        mail_spool_dir=tmp_path / "mail", storage_dir=tmp_path / "files",
    )
    with TestClient(create_app(settings)) as client:
        yield client


def test_wizard_served_with_local_assets_and_browser_guards(shell_client):
    response = shell_client.get("/precheck")
    assert response.status_code == 200
    assert "補助預檢" in response.text
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["referrer-policy"] == "no-referrer"
    assert shell_client.get("/static/app.js").status_code == 200
    assert shell_client.get("/static/styles.css").status_code == 200


def test_anonymous_responses_are_never_shared_cached(shell_client):
    for response in [shell_client.get("/api/v1/precheck/catalog"),
                     shell_client.post("/api/v1/precheck/evaluate", json={}),
                     shell_client.post("/api/v1/precheck/evaluate", json={"passed": True})]:
        assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("streamed", [False, True])
def test_precheck_limits_actual_body_size(shell_client, streamed):
    content = iter([b" " * 9000, b" " * 9000]) if streamed else b" " * 18000
    response = shell_client.post("/api/v1/precheck/evaluate", content=content,
                                 headers={"Content-Type": "application/json"})
    assert response.status_code == 413


@pytest.mark.parametrize("value", ["javascript:alert(1)", "https://name:password@example.test",
                                    "http://example.test", "https://", "https://example.test:bad"])
def test_official_link_requires_configured_https_without_credentials(value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, precheck_official_application_url=value,
                 secret_key="precheck-shell-test-" * 4,
                 totp_encryption_key=Fernet.generate_key().decode())
