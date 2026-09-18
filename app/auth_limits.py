"""Shared database rate limits; atomic across API workers and processes."""

from datetime import datetime, timedelta
import hashlib

from sqlalchemy import Integer, String, case
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from .common import ApiError
from .db import Base, UTCDateTime


class AuthRateLimit(Base):
    __tablename__ = "auth_rate_limits"
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    counter: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


def consume_limits(db: Session, rules: list[tuple[str, int, int]], now: datetime) -> None:
    """Count every attempt, including denied attempts, before other writes begin."""
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        insert = pg_insert
    elif dialect == "sqlite":
        insert = sqlite_insert
    else:
        raise ApiError(503, "AUTH_LIMITER_UNAVAILABLE", "帳號驗證服務暫時無法使用。")
    retry_after = 0
    for scope, window_seconds, limit in rules:
        key_hash = hashlib.sha256(scope.encode()).hexdigest()
        expires_at = now + timedelta(seconds=window_seconds)
        statement = insert(AuthRateLimit).values(key_hash=key_hash, counter=1, expires_at=expires_at)
        statement = statement.on_conflict_do_update(
            index_elements=[AuthRateLimit.key_hash],
            set_={
                "counter": case((AuthRateLimit.expires_at <= now, 1), else_=AuthRateLimit.counter + 1),
                "expires_at": case((AuthRateLimit.expires_at <= now, expires_at), else_=AuthRateLimit.expires_at),
                "updated_at": now,
            },
        ).returning(AuthRateLimit.counter, AuthRateLimit.expires_at)
        counter, expires_at = db.execute(statement).one()
        if counter > limit:
            retry_after = max(retry_after, max(1, int((expires_at - now).total_seconds()) + 1))
    db.commit()
    if retry_after:
        error = ApiError(429, "RATE_LIMITED", f"操作過於頻繁，請於 {retry_after} 秒後再試。")
        error.headers = {"Retry-After": str(retry_after)}
        raise error


def expired_before(now: datetime) -> datetime:
    """A housekeeping worker may delete rows older than this safe cutoff."""
    return now - timedelta(hours=1)
