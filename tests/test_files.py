"""File storage and authorization regression tests."""

import asyncio
from types import SimpleNamespace

import pytest

from app.storage import (
    StorageError,
    inspect_object,
    private_path,
    scan_object,
    store_once,
)


PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


async def chunks(data):
    yield data[:5]
    yield data[5:]


def test_uploaded_object_cannot_be_overwritten(tmp_path):
    key = "a" * 32
    result = asyncio.run(store_once(tmp_path, key, chunks(PDF_BYTES), len(PDF_BYTES), "application/pdf"))
    assert result[0] == len(PDF_BYTES)
    with pytest.raises(StorageError, match="UPLOAD_ALREADY_USED"):
        asyncio.run(store_once(tmp_path, key, chunks(b"%PDF-other"), 10, "application/pdf"))
    assert private_path(tmp_path, key).read_bytes() == PDF_BYTES
    assert inspect_object(tmp_path, key)[1] == result[1]


def test_magic_type_mismatch_is_not_persisted(tmp_path):
    with pytest.raises(StorageError, match="FILE_TYPE_MISMATCH"):
        asyncio.run(store_once(tmp_path, "b" * 32, chunks(PDF_BYTES), len(PDF_BYTES), "image/png"))
    assert not private_path(tmp_path, "b" * 32).exists()


def test_streamed_size_limit_cannot_be_bypassed(tmp_path):
    with pytest.raises(StorageError, match="FILE_TOO_LARGE"):
        asyncio.run(store_once(tmp_path, "c" * 32, chunks(PDF_BYTES), 5, "application/pdf"))
    assert not private_path(tmp_path, "c" * 32).exists()


def test_storage_key_rejects_path_traversal(tmp_path):
    with pytest.raises(StorageError, match="INVALID_OBJECT_KEY"):
        private_path(tmp_path, "../escape")


def test_production_scanner_fails_closed_even_with_invalid_settings(tmp_path):
    path = tmp_path / "sample"
    path.write_bytes(PDF_BYTES)
    settings = SimpleNamespace(app_env="production", scan_backend="development")
    result = scan_object(path, settings)
    assert result.status == "SCAN_FAILED"
    assert result.error == "DEV_SCANNER_FORBIDDEN"


def test_development_scanner_is_explicitly_not_antivirus(tmp_path):
    path = tmp_path / "sample"
    path.write_bytes(PDF_BYTES)
    settings = SimpleNamespace(app_env="test", scan_backend="development")
    result = scan_object(path, settings)
    assert result.status == "CLEAN"
    assert "NOT-ANTIVIRUS" in result.engine


def test_unconfigured_clamav_never_marks_clean(tmp_path):
    path = tmp_path / "sample"
    path.write_bytes(PDF_BYTES)
    result = scan_object(path, SimpleNamespace(scan_backend="clamav", clamav_host=None))
    assert result.status == "SCAN_FAILED"


@pytest.fixture
def file_api(tmp_path):
    from delivery_support import delivery_environment
    client, factory, settings, engine = delivery_environment(tmp_path)
    with client:
        yield client, factory, settings
    engine.dispose()


def _upload(client):
    response = client.post("/api/v1/files/upload-intents", json={
        "case_id": "case", "file_name": "付款證明.pdf", "size_bytes": len(PDF_BYTES),
        "content_type": "application/pdf",
    })
    assert response.status_code == 201, response.text
    intent = response.json()["data"]
    uploaded = client.put(intent["upload_url"], headers=intent["upload_headers"], content=PDF_BYTES)
    assert uploaded.status_code == 200, uploaded.text
    return intent


def test_api_upload_permission_token_and_immutable_storage(file_api):
    client, factory, settings = file_api
    denied = client.post("/api/v1/files/upload-intents", headers={"x-test-actor": "intruder"}, json={
        "case_id": "case", "file_name": "proof.pdf", "size_bytes": len(PDF_BYTES), "content_type": "application/pdf",
    })
    assert denied.status_code == 404
    intent = _upload(client)
    forbidden = client.put(intent["upload_url"], headers={**intent["upload_headers"], "x-test-actor": "intruder"}, content=PDF_BYTES)
    assert forbidden.status_code == 404
    repeated = client.put(intent["upload_url"], headers=intent["upload_headers"], content=PDF_BYTES)
    assert repeated.status_code == 409
    completed = client.post(f"/api/v1/files/{intent['file_id']}/complete", json={"file_version_id": intent["file_version_id"]})
    assert completed.status_code == 200
    assert completed.json()["data"]["scan_status"] == "PENDING_SCAN"
    after_complete = client.put(intent["upload_url"], headers=intent["upload_headers"], content=PDF_BYTES)
    assert after_complete.status_code in {403, 409}


def test_only_clean_files_download_and_revocation_applies_to_old_urls(file_api):
    from app.db import utcnow
    from app.models import CaseAccess
    from app.worker import process_scans
    client, factory, settings = file_api
    intent = _upload(client)
    client.post(f"/api/v1/files/{intent['file_id']}/complete", json={"file_version_id": intent["file_version_id"]})
    url = f"/api/v1/files/{intent['file_id']}/download"
    assert client.get(url).status_code == 409
    assert process_scans(factory, settings) == 1
    response = client.get(url)
    assert response.status_code == 200 and response.content == PDF_BYTES
    assert response.headers["cache-control"] == "private, no-store"
    assert "attachment" in response.headers["content-disposition"]
    assert client.get(url, headers={"Range": "bytes=0-3"}).status_code == 416
    assert client.get(url, headers={"x-test-actor": "admin"}).status_code == 404
    with factory() as db:
        db.get(CaseAccess, "ownership").revoked_at = utcnow()
        db.commit()
    assert client.get(url).status_code == 404
    assert client.get(url, headers={"Range": "bytes=0-3"}).status_code == 404
