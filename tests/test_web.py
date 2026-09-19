"""Public/admin entry boundaries, private paths and real static response semantics."""

from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.main import create_app


def web_settings(tmp_path, **overrides):
    values = dict(
        app_env="test", _env_file=None, database_url=f"sqlite:///{tmp_path / 'web.db'}",
        secret_key="web-test-secret-" * 4, totp_encryption_key=Fernet.generate_key().decode(),
        frontend_dist=tmp_path / "dist",
    )
    return Settings(**(values | overrides))


@pytest.fixture
def web(tmp_path):
    root = tmp_path / "dist"
    files = {
        "index.html": b"<!doctype html><title>Public entry</title><main>Citizen shell</main>",
        "admin/index.html": b"<!doctype html><title>Admin entry</title><main>Staff login shell</main>",
        "assets/public-a1b2c3d4.js": b"window.PUBLIC_ENTRY=true;",
        "assets/admin-e5f6g7h8.css": b"body{color:#123}",
        "assets/unversioned.js": b"window.VERSION=1;",
        "ocr/worker.min.js": b"self.onmessage=()=>{};",
        "ocr/tesseract-core-lstm.wasm": b"\x00asm\x01\x00\x00\x00",
        "ocr/chi_tra.traineddata": b"traditional-chinese-model-fixture",
        "ocr/eng.traineddata": b"english-model-fixture",
        "ocr/.hidden": b"not-public",
        "ai-safety-card.html": b"<!doctype html><title>Safety card</title>",
        "ai-safety-card.pdf": b"%PDF-1.7 test fixture",
        "ai-safety-card.png": b"\x89PNG\r\n\x1a\n",
    }
    for name, value in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)
    settings = web_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        yield client, root


def test_separate_public_and_admin_html_and_head(web):
    client, _ = web
    public = client.get("/")
    admin = client.get("/admin/")
    assert public.status_code == admin.status_code == 200
    assert "Citizen shell" in public.text and "Staff login shell" not in public.text
    assert "Staff login shell" in admin.text and "Citizen shell" not in admin.text
    for path in ["/", "/admin/"]:
        response = client.head(path)
        assert response.status_code == 200 and response.content == b""
        assert response.headers["content-type"].startswith("text/html")
        assert response.headers["cache-control"] == "no-store"


def test_admin_canonical_redirect_preserves_query(web):
    client, _ = web
    response = client.get("/admin?next=queue", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == "/admin/?next=queue"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["/cases/case-123", "/tasks/task_123", "/cases/12345678-1234-1234-1234-123456789012"])
def test_notification_paths_load_only_the_public_shell(web, path):
    client, _ = web
    response = client.get(path)
    assert response.status_code == 200
    assert "Citizen shell" in response.text
    assert "Staff login shell" not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", [
    "/unknown", "/admin/unknown", "/admin/assets/missing.js", "/cases", "/tasks",
    "/cases/case-123/extra", "/tasks/task-123/edit", "/cases/not-a-file.js",
    "/assets/missing.js", "/ocr/missing.wasm", "/api/v1/missing", "/health/missing",
])
def test_unknown_paths_do_not_fall_back_to_html(web, path):
    client, _ = web
    response = client.get(path)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "HTTP_ERROR"


def test_static_files_cache_mime_and_conditional_requests(web):
    client, _ = web
    script = client.get("/assets/public-a1b2c3d4.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    assert script.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "must-revalidate" in client.get("/assets/unversioned.js").headers["cache-control"]
    worker = client.get("/ocr/worker.min.js")
    assert "javascript" in worker.headers["content-type"]
    assert "must-revalidate" in worker.headers["cache-control"]
    model = client.get("/ocr/chi_tra.traineddata")
    assert model.status_code == 200
    assert model.headers["content-type"] == "application/octet-stream"
    assert "must-revalidate" in model.headers["cache-control"]
    cached = client.get("/ocr/chi_tra.traineddata", headers={"If-None-Match": model.headers["etag"]})
    assert cached.status_code == 304 and cached.content == b""
    assert cached.headers["cache-control"] == model.headers["cache-control"]
    wasm = client.get("/ocr/tesseract-core-lstm.wasm")
    assert wasm.headers["content-type"] == "application/wasm"
    assert wasm.content.startswith(b"\x00asm")
    partial = client.get("/ocr/tesseract-core-lstm.wasm", headers={"Range": "bytes=0-3"})
    assert partial.status_code == 206 and partial.content == b"\x00asm"
    assert client.head("/ocr/worker.min.js").content == b""


@pytest.mark.parametrize("extension,mime", [("html", "text/html"), ("pdf", "application/pdf"), ("png", "image/png")])
def test_only_named_safety_downloads_are_public(web, extension, mime):
    client, _ = web
    response = client.get(f"/ai-safety-card.{extension}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(mime)
    assert "must-revalidate" in response.headers["cache-control"]
    assert client.get("/ai-safety-card.docx").status_code == 404


@pytest.mark.parametrize("path", [
    "/.env", "/pyproject.toml", "/var/files/private.pdf", "/app/config.py",
    "/assets/%2e%2e/index.html", "/ocr/%2e%2e/admin/index.html", "/ocr/.hidden",
    "/assets/%2Fetc/passwd", "/assets/", "/frontend/dist/index.html",
])
def test_private_and_traversal_paths_are_not_exposed(web, path):
    client, _ = web
    assert client.get(path).status_code == 404


def test_symlinks_cannot_escape_public_asset_boundary(web, tmp_path):
    client, root = web
    private = tmp_path / "private.txt"
    private.write_text("private data", encoding="utf-8")
    (root / "assets" / "escape.txt").symlink_to(private)
    (root / "assets" / "cross-boundary.html").symlink_to(root / "admin" / "index.html")
    assert client.get("/assets/escape.txt").status_code == 404
    assert client.get("/assets/cross-boundary.html").status_code == 404


def test_public_html_does_not_grant_api_permissions_or_mask_api_errors(web):
    client, _ = web
    assert client.get("/admin/").status_code == 200
    response = client.get("/api/v1/me")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "private, no-store"
    assert client.get("/health/live").json()["data"]["status"] == "ok"
    assert client.post("/admin/").status_code == 405
    assert client.post("/cases/case-123").status_code == 405


def test_security_headers_apply_without_breaking_workers_or_liff(web):
    client, _ = web
    for path in ["/", "/admin/", "/ocr/worker.min.js", "/ocr/tesseract-core-lstm.wasm"]:
        response = client.get(path)
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-request-id"]
        assert "content-security-policy" not in response.headers
        assert "cross-origin-embedder-policy" not in response.headers


def test_absent_build_is_explicit_while_api_still_works(tmp_path):
    with TestClient(create_app(web_settings(tmp_path))) as client:
        for path in ["/", "/admin/", "/cases/case-123"]:
            response = client.get(path)
            assert response.status_code == 503
            assert "前端尚未建置" in response.json()["error"]["message"]
        assert client.get("/assets/missing.js").status_code == 404
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 200


def test_dist_setting_can_be_overridden_without_changing_cwd(tmp_path, monkeypatch):
    directory = tmp_path / "custom-public-build"
    monkeypatch.setenv("YOUTH_FRONTEND_DIST", str(directory))
    settings = web_settings(tmp_path, frontend_dist=Path(directory))
    assert settings.frontend_dist == directory
    # Omitting the Python override exercises Pydantic's YOUTH_ environment loading.
    values = settings.model_dump(exclude={"frontend_dist"})
    assert Settings(_env_file=None, **values).frontend_dist == directory
