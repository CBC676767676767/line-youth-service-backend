from sqlalchemy import select

from app.common import encode
from app.config import Settings
from app.db import Base, make_engine, make_session_factory, utcnow
from app.models import Scheme


def test_savepoint_must_not_survive_outer_rollback(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'rollback.db'}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        db.execute(select(Scheme))
        with db.begin_nested():
            db.add(Scheme(id="must-rollback", name="test", form_schema={}, config={}))
            db.flush()
        db.rollback()
    with factory() as db:
        assert db.get(Scheme, "must-rollback") is None
    engine.dispose()


def test_api_dates_use_utc_z():
    assert encode({"at": utcnow()})["at"].endswith("Z")


def test_production_refuses_development_adapters():
    import pytest
    with pytest.raises(ValueError):
        Settings(app_env="production", _env_file=None, secret_key="test" * 10)


def test_main_routes_health_body_limits_and_cors(tmp_path):
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient
    from app.main import create_app
    settings = Settings(app_env="test", _env_file=None, database_url=f"sqlite:///{tmp_path / 'main.db'}",
                        secret_key="testing-secret-" * 4, totp_encryption_key=Fernet.generate_key().decode(),
                        allowed_origins=["https://liff.example.org"])
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/ready").status_code == 200
        assert "/api/v1/webhooks/line" in client.get("/openapi.json").json()["paths"]
        preflight = client.options("/api/v1/files/a/content", headers={
            "Origin": "https://liff.example.org", "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "X-Upload-Token,X-CSRF-Token"})
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "https://liff.example.org"
        oversized = client.post("/api/v1/auth/email/challenges", content=b"a" * 1_048_577)
        assert oversized.status_code == 413
        streamed = client.post("/api/v1/auth/email/challenges", content=iter([b"a" * 600_000, b"b" * 600_000]),
                               headers={"Content-Type": "application/json"})
        assert streamed.status_code == 413


def test_database_errors_do_not_escape_and_carry_parameters_into_logs(tmp_path):
    """A re-raised driver error would put bound parameters in the server traceback."""
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient
    from sqlalchemy.exc import OperationalError
    from app.main import create_app
    settings = Settings(app_env="test", _env_file=None, database_url=f"sqlite:///{tmp_path / 'dberr.db'}",
                        secret_key="testing-secret-" * 4, totp_encryption_key=Fernet.generate_key().decode())
    application = create_app(settings)

    @application.get("/_probe_database_error")
    def probe():
        raise OperationalError("SELECT * FROM accounts WHERE email=?",
                               {"email": "applicant@example.test"}, Exception("connection lost"))

    # raise_server_exceptions stays on: escaping the app would fail this call, not return 500.
    with TestClient(application) as client:
        response = client.get("/_probe_database_error")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "applicant@example.test" not in response.text
