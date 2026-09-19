"""Append-only precheck snapshots without changing existing case data.

Revision ID: c89f7a621b34
Revises: 78a60fc3915a
"""

from alembic import op
import sqlalchemy as sa

from app.db import UTCDateTime


revision = "c89f7a621b34"
down_revision = "78a60fc3915a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "precheck_snapshots",
        sa.Column("case_id", sa.String(36), sa.ForeignKey("cases.id"), nullable=False),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("previous_snapshot_id", sa.String(36), sa.ForeignKey("precheck_snapshots.id")),
        sa.Column("input_version", sa.String(40), nullable=False),
        sa.Column("rules_version", sa.String(100), nullable=False),
        sa.Column("catalog_version", sa.String(100), nullable=False),
        sa.Column("executed_at", UTCDateTime(), nullable=False),
        sa.Column("inputs", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("differences", sa.JSON(), nullable=False),
        sa.Column("configuration_snapshot", sa.JSON(), nullable=False),
        sa.Column("configuration_hash", sa.String(64), nullable=False),
        sa.Column("safety_tips", sa.JSON(), nullable=False),
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("case_id", "sequence", name="uq_precheck_case_sequence"),
    )
    op.create_index("ix_precheck_snapshots_case_id", "precheck_snapshots", ["case_id"])


def downgrade() -> None:
    op.drop_index("ix_precheck_snapshots_case_id", table_name="precheck_snapshots")
    op.drop_table("precheck_snapshots")
