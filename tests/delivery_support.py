"""Isolated API/DB fixtures shared by file and delivery regression tests."""

from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import Principal, current_principal
from app.config import Settings
from app.db import Base, get_db, make_engine, make_session_factory
from app.files import router as files_router
from app.models import Account, Case, CaseAccess, RoleGrant, Scheme
from app.notifications import router as notifications_router
from app.common import ApiError


def delivery_environment(tmp_path):
    settings = Settings(
        app_env="test", database_url=f"sqlite:///{tmp_path}/delivery.db",
        secret_key="delivery-test-secret-" * 3, totp_encryption_key=Fernet.generate_key().decode(),
        storage_dir=tmp_path / "files", line_provider_id="provider",
        line_messaging_channel_id="channel", line_destination_user_id="bot-id",
        line_channel_secret="webhook-test-secret",
    )
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory() as db:
        db.add_all([Account(id="owner", email="owner@example.test"),
                    Account(id="intruder", email="intruder@example.test"),
                    Account(id="admin", email="admin@example.test"),
                    Scheme(id="scheme", name="Test", config={"document_types": ["OTHER"]})])
        db.flush()
        db.add_all([RoleGrant(account_id="owner", role="applicant", scope_type="GLOBAL"),
                    RoleGrant(account_id="intruder", role="applicant", scope_type="GLOBAL"),
                    RoleGrant(account_id="admin", role="admin", scope_type="GLOBAL"),
                    Case(id="case", scheme_id="scheme", case_no="TEST-1", created_by="owner", status="DRAFT")])
        db.flush()
        db.add(CaseAccess(id="ownership", case_id="case", account_id="owner", permission="OWNER"))
        db.commit()
    application = FastAPI()
    application.state.settings = settings
    application.state.session_factory = factory

    @application.exception_handler(ApiError)
    async def api_error(request, exc):
        return JSONResponse({"error": {"code": exc.code}}, status_code=exc.status)

    def fixture_principal(request: Request, db=Depends(get_db)):
        account = db.get(Account, request.headers.get("x-test-actor", "owner"))
        grants = db.scalars(select(RoleGrant).where(RoleGrant.account_id == account.id)).all()
        return Principal(account=account, session=None, grants=grants, roles={g.role for g in grants})

    application.dependency_overrides[current_principal] = fixture_principal
    application.include_router(files_router, prefix="/api/v1")
    application.include_router(notifications_router, prefix="/api/v1")
    return TestClient(application), factory, settings, engine
