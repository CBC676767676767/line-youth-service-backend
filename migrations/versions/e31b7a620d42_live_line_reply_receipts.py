"""Allow live reply receipts without persisting tokens or message content.

Revision ID: e31b7a620d42
Revises: d76e14b290a1
"""

from alembic import op
import sqlalchemy as sa


revision = "e31b7a620d42"
down_revision = "d76e14b290a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("line_reply_jobs") as batch:
        batch.drop_constraint("ck_line_reply_mode", type_="check")
        batch.drop_constraint("ck_line_reply_status", type_="check")
        batch.create_check_constraint("ck_line_reply_mode", "delivery_mode IN ('fake', 'live')")
        batch.create_check_constraint(
            "ck_line_reply_status",
            "status IN ('PENDING', 'SENDING', 'FAKE_SENT', 'API_ACCEPTED', 'EXPIRED', 'UNKNOWN', 'FAILED')",
        )


def downgrade() -> None:
    # Keep truthful historical receipts: do not relabel real delivery as fake.
    count = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM line_reply_jobs WHERE delivery_mode = 'live' OR status = 'API_ACCEPTED'"
    )).scalar_one()
    if count:
        raise RuntimeError("Cannot downgrade while live LINE reply receipts exist")
    with op.batch_alter_table("line_reply_jobs") as batch:
        batch.drop_constraint("ck_line_reply_mode", type_="check")
        batch.drop_constraint("ck_line_reply_status", type_="check")
        batch.create_check_constraint("ck_line_reply_mode", "delivery_mode = 'fake'")
        batch.create_check_constraint(
            "ck_line_reply_status", "status IN ('PENDING', 'SENDING', 'FAKE_SENT', 'EXPIRED', 'UNKNOWN', 'FAILED')",
        )
