"""Durable reply receipts contain delivery metadata, never conversation inputs."""

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime


class LineReplyJob(Base):
    __tablename__ = "line_reply_jobs"

    channel_id: Mapped[str] = mapped_column(String(100))
    event_id: Mapped[str] = mapped_column(String(100))
    event_type: Mapped[str] = mapped_column(String(20))
    received_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    delivery_mode: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint("channel_id", "event_id", name="uq_line_reply_channel_event"),
        CheckConstraint("attempt_count >= 0 AND attempt_count <= 1", name="ck_line_reply_once"),
        CheckConstraint(
            "status IN ('PENDING', 'SENDING', 'FAKE_SENT', 'API_ACCEPTED', 'EXPIRED', 'UNKNOWN', 'FAILED')",
            name="ck_line_reply_status",
        ),
        CheckConstraint("delivery_mode IN ('fake', 'live')", name="ck_line_reply_mode"),
        Index("ix_line_reply_status_expires", "status", "expires_at"),
    )
