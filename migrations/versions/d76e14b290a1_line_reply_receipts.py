"""Add reply delivery receipts without storing chat state or credentials.

Revision ID: d76e14b290a1
Revises: c89f7a621b34
"""

from alembic import op
import sqlalchemy as sa

from app.db import UTCDateTime


revision = "d76e14b290a1"
down_revision = "c89f7a621b34"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "line_reply_jobs",
        sa.Column("channel_id", sa.String(100), nullable=False),
        sa.Column("event_id", sa.String(100), nullable=False),
        sa.Column("event_type", sa.String(20), nullable=False),
        sa.Column("received_at", UTCDateTime(), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=False),
        sa.Column("delivery_mode", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("started_at", UTCDateTime()),
        sa.Column("completed_at", UTCDateTime()),
        sa.Column("http_status", sa.Integer()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("channel_id", "event_id", name="uq_line_reply_channel_event"),
        sa.CheckConstraint("attempt_count >= 0 AND attempt_count <= 1", name="ck_line_reply_once"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'SENDING', 'FAKE_SENT', 'EXPIRED', 'UNKNOWN', 'FAILED')",
            name="ck_line_reply_status",
        ),
        sa.CheckConstraint("delivery_mode = 'fake'", name="ck_line_reply_mode"),
    )
    op.create_index("ix_line_reply_status_expires", "line_reply_jobs", ["status", "expires_at"])


def downgrade() -> None:
    op.drop_index("ix_line_reply_status_expires", table_name="line_reply_jobs")
    op.drop_table("line_reply_jobs")
