"""The schema. Frozen after Wave 0 of the build.

Ten tables. Several requirements are satisfied by a constraint rather than by
code, which is deliberate: application logic can be bypassed by a future
endpoint, a unique index cannot. Those constraints are commented with the
requirement they enforce.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.db.types import ExactDecimal, UtcDateTime


class Base(DeclarativeBase):
    pass


class Source(Base):
    """One system that sends us data. A third source is one row here."""

    __tablename__ = "source"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    # Column map, timestamp patterns, timezone, vocabulary maps. TR-201, TR-207.
    format_config: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())


class ToleranceProfile(Base):
    """Thresholds for one source pair. No threshold is a literal in code. TR-405."""

    __tablename__ = "tolerance_profile"
    __table_args__ = (UniqueConstraint("left_source_id", "right_source_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    left_source_id: Mapped[int] = mapped_column(ForeignKey("source.id"))
    right_source_id: Mapped[int] = mapped_column(ForeignKey("source.id"))
    amount_bps: Mapped[Decimal] = mapped_column(ExactDecimal(scale=12))
    amount_abs_floor: Mapped[Decimal] = mapped_column(ExactDecimal(scale=12))
    price_bps: Mapped[Decimal] = mapped_column(ExactDecimal(scale=12))
    qty_bps: Mapped[Decimal] = mapped_column(ExactDecimal(scale=12))
    time_tolerance_seconds: Mapped[int] = mapped_column(Integer)
    suggest_window_seconds: Mapped[int] = mapped_column(Integer)


class FileBatch(Base):
    """One accepted delivery. A correction is a new version of the same period."""

    __tablename__ = "file_batch"
    __table_args__ = (
        # A byte-identical resend cannot be accepted. TR-102, TR-103, TR-503.
        UniqueConstraint("source_id", "content_hash", name="uq_batch_source_hash"),
        Index("ix_batch_source_period", "source_id", "period_start", "period_end"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id"))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    filename: Mapped[str] = mapped_column(String(255))
    content_hash: Mapped[str] = mapped_column(String(64))
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    superseded_by_id: Mapped[int | None] = mapped_column(ForeignKey("file_batch.id"))
    accepted_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)

    source: Mapped[Source] = relationship()


class Record(Base):
    """One normalised row. Never updated, never deleted. TR-501.

    A correction writes new rows under a new batch; the old rows survive under
    the superseded batch, which is how value history is answered.
    """

    __tablename__ = "record"
    __table_args__ = (
        Index("ix_record_identity", "source_id", "reference"),
        Index("ix_record_batch", "batch_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("file_batch.id"))
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id"))
    reference: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime)
    instrument: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[Decimal] = mapped_column(ExactDecimal(scale=12))
    unit_price: Mapped[Decimal] = mapped_column(ExactDecimal(scale=12))
    gross_amount: Mapped[Decimal] = mapped_column(ExactDecimal(scale=12))
    status: Mapped[str] = mapped_column(String(16))
    is_cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    row_no: Mapped[int] = mapped_column(Integer)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON)

    batch: Mapped[FileBatch] = relationship()


class RejectedRow(Base):
    """A row that could not be loaded. Never blocks the rest of its file. TR-106."""

    __tablename__ = "rejected_row"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("file_batch.id"), index=True)
    row_no: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON)


class Run(Base):
    """One reconciliation. Append-only. TR-502."""

    __tablename__ = "run"

    id: Mapped[int] = mapped_column(primary_key=True)
    left_source_id: Mapped[int] = mapped_column(ForeignKey("source.id"))
    right_source_id: Mapped[int] = mapped_column(ForeignKey("source.id"))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Resolution(Base):
    """A durable decision, keyed on business identity rather than row id.

    Records are immutable, so a correction writes new record rows. A resolution
    pointing at a record id would silently detach exactly when the analyst most
    needs it to hold. See DESIGN.md DD-5 and CLAUDE.md invariant 4.
    """

    __tablename__ = "resolution"
    __table_args__ = (
        Index("ix_resolution_left", "left_source_id", "left_reference"),
        Index("ix_resolution_right", "right_source_id", "right_reference"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    left_source_id: Mapped[int] = mapped_column(ForeignKey("source.id"))
    left_reference: Mapped[str] = mapped_column(String(64))
    right_source_id: Mapped[int | None] = mapped_column(ForeignKey("source.id"))
    right_reference: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    author: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, server_default=func.now())
    # Revocation is recorded, never deleted. TR-708.
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    revoked_reason: Mapped[str | None] = mapped_column(Text)


class Pair(Base):
    """Two records believed to describe the same transaction."""

    __tablename__ = "pair"
    __table_args__ = (
        # One-to-one, enforced by the database and not only by code. TR-311, TR-504.
        UniqueConstraint("run_id", "left_record_id", name="uq_pair_run_left"),
        UniqueConstraint("run_id", "right_record_id", name="uq_pair_run_right"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("run.id"), index=True)
    left_record_id: Mapped[int] = mapped_column(ForeignKey("record.id"))
    right_record_id: Mapped[int] = mapped_column(ForeignKey("record.id"))
    origin: Mapped[str] = mapped_column(String(16))
    verdict: Mapped[str] = mapped_column(String(24))
    resolution_id: Mapped[int | None] = mapped_column(ForeignKey("resolution.id"))
    # Denormalised so the worklist sorts by size of difference in SQL. TR-606.
    max_rel_diff: Mapped[Decimal] = mapped_column(ExactDecimal(scale=12), default=Decimal(0))


class FieldDiffRow(Base):
    """One compared field of one pair.

    A table rather than a JSON blob on ``pair`` because the worklist orders by
    size of difference, and sorting inside a blob means a generated column or a
    full scan in Python. See DESIGN.md section 2.
    """

    __tablename__ = "field_diff"

    id: Mapped[int] = mapped_column(primary_key=True)
    pair_id: Mapped[int] = mapped_column(ForeignKey("pair.id"), index=True)
    field: Mapped[str] = mapped_column(String(32))
    left_value: Mapped[str] = mapped_column(String(64))
    right_value: Mapped[str] = mapped_column(String(64))
    differs: Mapped[bool] = mapped_column(Boolean)
    within_tolerance: Mapped[bool] = mapped_column(Boolean)
    abs_diff: Mapped[Decimal | None] = mapped_column(ExactDecimal(scale=12))
    rel_diff: Mapped[Decimal | None] = mapped_column(ExactDecimal(scale=12))


class RunItem(Base):
    """One row per record per run, carrying exactly one state.

    Makes "every record read is in exactly one state" a property of the schema,
    and the run summary a single GROUP BY. TR-509.
    """

    __tablename__ = "run_item"
    __table_args__ = (
        UniqueConstraint("run_id", "record_id", name="uq_run_item"),
        Index("ix_run_item_worklist", "run_id", "state"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("run.id"))
    record_id: Mapped[int] = mapped_column(ForeignKey("record.id"))
    side: Mapped[str] = mapped_column(String(8))
    state: Mapped[str] = mapped_column(String(32))
    pair_id: Mapped[int | None] = mapped_column(ForeignKey("pair.id"))
