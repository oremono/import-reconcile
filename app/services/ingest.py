"""Loading a file: hashing, duplicate refusal, period rules, and versioning.

Three things arrive at this door and only one is ordinary. A new file loads. A
byte-identical resend must be refused, because double-counting a statement is
worse than any error message. A file restating a period already received is a
correction, and its values become authoritative while the previous ones stay
answerable.

Record rows are never updated and never deleted. A correction writes new rows
under a new batch and marks the old batch superseded, which is what makes
history a property of the schema rather than a feature bolted onto it
(CLAUDE.md invariant 3).
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import FileBatch, Record, RejectedRow, Source
from app.observability import log_ingest
from core.format import source_format_from_config
from core.model import NormalizedRecord, RowError
from core.normalize import HeaderError, normalize_rows

# ---------------------------------------------------------------------------
# Failures the web layer renders as messages, never as a 500 (TR-605)
# ---------------------------------------------------------------------------


class IngestError(Exception):
    """A file that cannot be accepted, for a reason a person can act on."""


class DuplicateFileError(IngestError):
    """This exact file has already been accepted for this source (TR-102)."""

    def __init__(self, filename: str, accepted_at: object, version_no: int) -> None:
        super().__init__(
            f"Already accepted on {accepted_at} as version {version_no} "
            f"({filename}). Nothing has changed."
        )
        self.accepted_at = accepted_at
        self.version_no = version_no


class OverlappingPeriodError(IngestError):
    """A period that partly overlaps one already loaded (TR-104, D21)."""

    def __init__(self, start: date, end: date, other_start: date, other_end: date) -> None:
        super().__init__(
            f"Period {start} to {end} partly overlaps {other_start} to {other_end}, "
            "which is already loaded. A file must restate a period exactly or not "
            "overlap it at all."
        )


class MalformedFileError(IngestError):
    """Not readable as the CSV this source is configured to send (TR-101)."""


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IngestResult:
    """What the web layer needs to tell the analyst what happened."""

    batch_id: int
    source_code: str
    version_no: int
    accepted_rows: int
    rejected_rows: int
    superseded_batch_id: int | None
    withdrawn_references: tuple[str, ...]

    @property
    def is_correction(self) -> bool:
        return self.superseded_batch_id is not None

    @property
    def summary(self) -> str:
        what = "Correction accepted" if self.is_correction else "File accepted"
        detail = f"{self.accepted_rows} rows"
        if self.rejected_rows:
            detail += f", {self.rejected_rows} could not be read"
        if self.withdrawn_references:
            detail += f", {len(self.withdrawn_references)} withdrawn"
        return f"{what} as version {self.version_no}: {detail}."


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def content_digest(content: bytes) -> str:
    """SHA-256 of the raw bytes.

    Of the contents, never the filename: filenames are unreliable and are often
    stamped with the send time, so two deliveries of the same data routinely
    arrive under different names (D6).
    """
    return hashlib.sha256(content).hexdigest()


def read_rows(content: bytes) -> tuple[list[dict[str, str]], list[str]]:
    """Decode and parse. Raises MalformedFileError if it is not CSV at all."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MalformedFileError(f"File is not UTF-8 text: {exc}") from exc

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise MalformedFileError("File is empty, or has no header row.")
    rows = [{k: (v or "") for k, v in row.items() if k is not None} for row in reader]
    return rows, list(reader.fieldnames)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def _refuse_duplicate(session: Session, source: Source, digest: str) -> None:
    existing = session.scalar(
        select(FileBatch).where(FileBatch.source_id == source.id, FileBatch.content_hash == digest)
    )
    if existing is not None:
        raise DuplicateFileError(existing.filename, existing.accepted_at, existing.version_no)


def _refuse_partial_overlap(session: Session, source: Source, start: date, end: date) -> None:
    """Exact match or no overlap. Anything between is refused (D21).

    Superseding only the overlap would break the rule that a file is a complete
    restatement of its period, and would make a withdrawn row undetectable.
    """
    clashing = session.scalars(
        select(FileBatch).where(
            FileBatch.source_id == source.id,
            FileBatch.period_start <= end,
            FileBatch.period_end >= start,
        )
    )
    for batch in clashing:
        if (batch.period_start, batch.period_end) != (start, end):
            raise OverlappingPeriodError(start, end, batch.period_start, batch.period_end)


def _current_batch(session: Session, source: Source, start: date, end: date) -> FileBatch | None:
    return session.scalar(
        select(FileBatch).where(
            FileBatch.source_id == source.id,
            FileBatch.period_start == start,
            FileBatch.period_end == end,
            FileBatch.superseded_by_id.is_(None),
        )
    )


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


def ingest_file(
    session: Session,
    source: Source,
    content: bytes,
    period_start: date,
    period_end: date,
    filename: str,
) -> IngestResult:
    """Accept one delivery, or refuse it with a reason a person can act on.

    Atomic: either the batch, its records and its rejected rows all land, or
    nothing does (TR-108). The caller owns the transaction, so an exception
    escaping here leaves no trace once it rolls back.
    """
    digest = content_digest(content)
    _refuse_duplicate(session, source, digest)
    _refuse_partial_overlap(session, source, period_start, period_end)

    fmt = source_format_from_config(source.code, source.format_config)
    rows, header = read_rows(content)
    try:
        from core.normalize import validate_header

        validate_header(header, fmt)
    except HeaderError as exc:
        raise MalformedFileError(str(exc)) from exc

    records, errors = normalize_rows(rows, fmt)

    previous = _current_batch(session, source, period_start, period_end)
    version_no = previous.version_no + 1 if previous else 1

    batch = FileBatch(
        source_id=source.id,
        period_start=period_start,
        period_end=period_end,
        filename=filename,
        content_hash=digest,
        version_no=version_no,
        row_count=len(records),
        rejected_count=len(errors),
    )
    session.add(batch)
    session.flush()

    _persist_records(session, batch, source, records)
    _persist_errors(session, batch, errors)

    withdrawn: tuple[str, ...] = ()
    if previous is not None:
        # Marking the old batch superseded is what makes "current" mean
        # something. The records under it are untouched.
        previous.superseded_by_id = batch.id
        withdrawn = tuple(
            sorted(
                {r.reference for r in _records_of(session, previous)}
                - {r.reference for r in records}
            )
        )

    session.flush()
    result = IngestResult(
        batch_id=batch.id,
        source_code=source.code,
        version_no=version_no,
        accepted_rows=len(records),
        rejected_rows=len(errors),
        superseded_batch_id=previous.id if previous else None,
        withdrawn_references=withdrawn,
    )
    # TR-706. What arrived, for which period, as which version, and what it
    # cost. Emitted only on the path that actually wrote something: a refusal
    # raises above this line and leaves no trace, which is the point.
    log_ingest(
        source=result.source_code,
        period_start=period_start,
        period_end=period_end,
        version=result.version_no,
        batch_id=result.batch_id,
        accepted_rows=result.accepted_rows,
        rejected_rows=result.rejected_rows,
        withdrawn=len(result.withdrawn_references),
        superseded_batch_id=result.superseded_batch_id,
    )
    return result


def _persist_records(
    session: Session, batch: FileBatch, source: Source, records: tuple[NormalizedRecord, ...]
) -> None:
    session.add_all(
        Record(
            batch_id=batch.id,
            source_id=source.id,
            reference=record.reference,
            occurred_at=record.occurred_at,
            instrument=record.instrument,
            side=str(record.side),
            quantity=record.quantity,
            unit_price=record.unit_price,
            gross_amount=record.gross_amount,
            status=str(record.status),
            is_cancelled=record.is_cancelled,
            row_no=record.row_no,
            raw=dict(record.raw),
        )
        for record in records
    )


def _persist_errors(session: Session, batch: FileBatch, errors: tuple[RowError, ...]) -> None:
    """A bad row is recorded and shown. It never costs the rest of the file (TR-106)."""
    session.add_all(
        RejectedRow(
            batch_id=batch.id,
            row_no=error.row_no,
            reason=error.reason,
            raw=dict(error.raw),
        )
        for error in errors
    )


def _records_of(session: Session, batch: FileBatch) -> list[Record]:
    return list(session.scalars(select(Record).where(Record.batch_id == batch.id)))


# ---------------------------------------------------------------------------
# Derived history
# ---------------------------------------------------------------------------


def current_batch(session: Session, source: Source, start: date, end: date) -> FileBatch | None:
    """The batch whose records count right now for this source and period."""
    return _current_batch(session, source, start, end)


def current_records(session: Session, source: Source, start: date, end: date) -> list[Record]:
    batch = _current_batch(session, source, start, end)
    return _records_of(session, batch) if batch else []


def withdrawn_references(session: Session, source: Source, start: date, end: date) -> set[str]:
    """References that were present in an earlier version and are gone now.

    Derived rather than stored, because storing it would mean mutating a record
    and records are immutable (TR-107, D8). A row vanishing between versions is
    itself a finding, so it is surfaced rather than silently dropped.
    """
    current = _current_batch(session, source, start, end)
    if current is None:
        return set()
    superseded = session.scalars(
        select(FileBatch).where(
            FileBatch.source_id == source.id,
            FileBatch.period_start == start,
            FileBatch.period_end == end,
            FileBatch.superseded_by_id.is_not(None),
        )
    )
    ever = {r.reference for batch in superseded for r in _records_of(session, batch)}
    return ever - {r.reference for r in _records_of(session, current)}


def load_source_file(path: Path) -> bytes:
    return path.read_bytes()
