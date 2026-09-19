"""Append-only precheck evidence, kept separate from formal review and finance."""

from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Integer, String, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime


class PrecheckSnapshot(Base):
    __tablename__ = "precheck_snapshots"

    case_id: Mapped[str] = mapped_column(ForeignKey("cases.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    sequence: Mapped[int] = mapped_column(Integer)
    previous_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("precheck_snapshots.id"))
    input_version: Mapped[str] = mapped_column(String(40))
    rules_version: Mapped[str] = mapped_column(String(100))
    catalog_version: Mapped[str] = mapped_column(String(100))
    executed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    inputs: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)
    differences: Mapped[dict] = mapped_column(JSON)
    configuration_snapshot: Mapped[dict] = mapped_column(JSON)
    configuration_hash: Mapped[str] = mapped_column(String(64))
    safety_tips: Mapped[list] = mapped_column(JSON)

    __table_args__ = (UniqueConstraint("case_id", "sequence", name="uq_precheck_case_sequence"),)


@event.listens_for(PrecheckSnapshot, "before_update")
@event.listens_for(PrecheckSnapshot, "before_delete")
def immutable_snapshot(_mapper, _connection, _target):
    raise ValueError("Precheck snapshots are immutable; create a new snapshot instead")
