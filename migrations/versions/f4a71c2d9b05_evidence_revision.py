"""Tie a review conclusion to the evidence revision it was reached against.

Existing rows start at 0 on both sides, so cases already in review keep their
conclusions instead of being forced into a re-review by the upgrade itself.

Revision ID: f4a71c2d9b05
Revises: e31b7a620d42
"""

from alembic import op
import sqlalchemy as sa


revision = "f4a71c2d9b05"
down_revision = "e31b7a620d42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cases", sa.Column("evidence_revision_no", sa.Integer(),
                                     nullable=False, server_default="0"))
    op.add_column("review_items", sa.Column("reviewed_evidence_revision", sa.Integer(),
                                            nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("review_items", "reviewed_evidence_revision")
    op.drop_column("cases", "evidence_revision_no")
