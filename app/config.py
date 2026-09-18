from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="YOUTH_", env_file=".env", extra="ignore")

    app_env: str = "development"
    database_url: str = "sqlite:///./var/youth.db"
    public_origin: str = "http://127.0.0.1:8000"
    allowed_origins: list[str] = ["http://127.0.0.1:8000", "http://localhost:8000"]
    secret_key: str = ""
    totp_encryption_key: str = ""
    secrets_file: Path = Path("var/development-secrets.json")
    session_cookie_name: str = "youth_session"
    cookie_secure: bool = False
    otp_ttl_seconds: int = 600
    otp_resend_seconds: int = 60
    otp_max_attempts: int = 5
    reauth_seconds: int = 600
    youth_session_seconds: int = 43200
    staff_session_seconds: int = 28800
    youth_idle_seconds: int = 1800
    staff_idle_seconds: int = 900
    mail_backend: str = "spool"
    mail_spool_dir: Path = Path("var/mail")
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    smtp_use_tls: bool = False
    mail_from: str = "no-reply@example.test"
    line_channel_id: str = ""
    line_provider_id: str = ""
    line_messaging_channel_id: str = ""
    line_channel_secret: str = ""
    line_channel_access_token: str = ""
    line_destination_user_id: str = ""
    storage_dir: Path = Path("var/files")
    scan_backend: str = "development"
    clamav_host: str = "127.0.0.1"
    clamav_port: int = 3310
    clamav_timeout: int = 30
    max_file_bytes: int = 20_971_520
    max_files_per_submission: int = 10
    case_storage_quota_bytes: int = 524_288_000
    upload_ttl_seconds: int = 600
    worker_poll_seconds: float = 2.0
    export_ttl_seconds: int = 86400
    auto_create_schema: bool = False

    @model_validator(mode="after")
    def validate_environment(self):
        if self.app_env not in {"development", "test", "production"}:
            raise ValueError("app_env must be development, test, or production")
        if self.app_env == "production":
            if len(self.secret_key) < 32 or not self.totp_encryption_key:
                raise ValueError("Production requires configured secret and TOTP encryption keys")
            if not self.cookie_secure or not self.public_origin.startswith("https://"):
                raise ValueError("Production requires HTTPS and secure cookies")
            if not self.allowed_origins or any(not origin.startswith("https://") or "*" in origin
                                               for origin in self.allowed_origins):
                raise ValueError("Production requires an explicit HTTPS origin allowlist")
            if self.scan_backend != "clamav" or self.mail_backend != "smtp":
                raise ValueError("Production requires ClamAV and SMTP, not development adapters")
            if self.database_url.startswith("sqlite") or self.auto_create_schema:
                raise ValueError("Production requires PostgreSQL and explicit Alembic migrations")
            if not self.database_url.startswith("postgresql"):
                raise ValueError("Production database must use PostgreSQL")
            if not self.smtp_host or not (self.smtp_use_tls or self.smtp_starttls):
                raise ValueError("Production requires a configured SMTP host and TLS")
        elif not self.secret_key or not self.totp_encryption_key:
            self.secrets_file.parent.mkdir(parents=True, exist_ok=True)
            values = {"secret_key": secrets.token_urlsafe(48),
                      "totp_encryption_key": Fernet.generate_key().decode()}
            try:
                fd = os.open(self.secrets_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as stream:
                    json.dump(values, stream)
            except FileExistsError:
                values = json.loads(self.secrets_file.read_text())
            self.secret_key = self.secret_key or values["secret_key"]
            self.totp_encryption_key = self.totp_encryption_key or values["totp_encryption_key"]
        if len(self.secret_key) < 32:
            raise ValueError("secret_key must have at least 32 characters")
        Fernet(self.totp_encryption_key.encode())
        if self.mail_backend not in {"spool", "smtp"} or self.scan_backend not in {"development", "clamav"}:
            raise ValueError("Unsupported mail or scan backend")
        if not 0 < self.max_file_bytes <= 20_971_520 or not 0 < self.max_files_per_submission <= 10:
            raise ValueError("File limits exceed the supported envelope")
        if self.otp_ttl_seconds <= 0 or self.otp_resend_seconds <= 0 or self.worker_poll_seconds <= 0:
            raise ValueError("Timeout and polling settings must be positive")
        return self
