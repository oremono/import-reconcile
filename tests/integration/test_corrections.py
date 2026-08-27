"""Corrections: new values win, old values stay answerable, dropped rows surface.

A correction is the case the schema was designed around. Records are immutable,
so a corrected file writes new rows under a new batch and marks the old batch
superseded. History is then a consequence of the design rather than a feature,
and "what did this row say before?" is a query rather than a hope.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import FileBatch, Record
from app.services.ingest import (
    current_records,
    ingest_file,
    withdrawn_references,
)

DATA = Path(__file__).resolve().parents[2] / "data"
PERIOD = (date(2025, 7, 1), date(2025, 7, 7))

STATEMENT = DATA / "statement_2025-07-01_07.csv"
CORRECTED = DATA / "statement_2025-07-01_07_v2.csv"

# From scripts/make_sample_data.py: three amounts fixed, C-9002 dropped.
ORIGINAL_ROWS = 40
CORRECTED_ROWS = 39
WITHDRAWN_REFERENCE = "C-9002"


def load(session: Session, source, path: Path):
    return ingest_file(session, source, path.read_bytes(), PERIOD[0], PERIOD[1], path.name)


def test_versioning(db_session: Session, seeded_sources) -> None:
    """TR-105. A correction is version 2, and version 1 becomes superseded."""
    first = load(db_session, seeded_sources.statement, STATEMENT)
    second = load(db_session, seeded_sources.statement, CORRECTED)

    assert first.version_no == 1
    assert second.version_no == 2
    assert second.is_correction
    assert second.superseded_batch_id == first.batch_id

    original = db_session.get(FileBatch, first.batch_id)
    assert original is not None
    assert original.superseded_by_id == second.batch_id


def test_the_corrected_values_are_the_ones_that_count(db_session: Session, seeded_sources) -> None:
    """AC8. After a correction the new amounts are what a run reads."""
    load(db_session, seeded_sources.statement, STATEMENT)
    before = {
        r.reference: r.gross_amount
        for r in current_records(db_session, seeded_sources.statement, *PERIOD)
    }

    load(db_session, seeded_sources.statement, CORRECTED)
    after = {
        r.reference: r.gross_amount
        for r in current_records(db_session, seeded_sources.statement, *PERIOD)
    }

    changed = {ref for ref, amount in after.items() if before.get(ref) != amount}
    assert len(changed) == 3, f"expected three corrected amounts, got {sorted(changed)}"


def test_the_previous_values_are_still_answerable(db_session: Session, seeded_sources) -> None:
    """R8.2. A person will still ask what the row used to say.

    Nothing is overwritten, so both versions of a corrected row are present and
    a query by (source, reference) returns the whole history in order.
    """
    load(db_session, seeded_sources.statement, STATEMENT)
    load(db_session, seeded_sources.statement, CORRECTED)

    corrected_reference = _first_corrected_reference(db_session, seeded_sources.statement)
    history = list(
        db_session.scalars(
            select(Record)
            .join(FileBatch, Record.batch_id == FileBatch.id)
            .where(
                Record.source_id == seeded_sources.statement.id,
                Record.reference == corrected_reference,
            )
            .order_by(FileBatch.version_no)
        )
    )
    assert len(history) == 2
    assert history[0].gross_amount != history[1].gross_amount


def test_records_are_never_mutated(db_session: Session, seeded_sources) -> None:
    """CLAUDE.md invariant 3, observed rather than asserted.

    A correction adds rows; it never edits them. Both versions survive, so the
    total is the sum of the two files rather than the size of the newer one.
    """
    load(db_session, seeded_sources.statement, STATEMENT)
    load(db_session, seeded_sources.statement, CORRECTED)
    total = db_session.scalar(select(func.count()).select_from(Record))
    assert total == ORIGINAL_ROWS + CORRECTED_ROWS


def test_withdrawn(db_session: Session, seeded_sources) -> None:
    """TR-107, D8. A row that vanishes between versions is itself a finding."""
    load(db_session, seeded_sources.statement, STATEMENT)
    result = load(db_session, seeded_sources.statement, CORRECTED)

    assert result.withdrawn_references == (WITHDRAWN_REFERENCE,)
    assert withdrawn_references(db_session, seeded_sources.statement, *PERIOD) == {
        WITHDRAWN_REFERENCE
    }


def test_withdrawal_is_derived_not_stored(db_session: Session, seeded_sources) -> None:
    """No column says 'withdrawn'. Storing one would mean mutating a record."""
    assert not hasattr(Record, "withdrawn_at")
    load(db_session, seeded_sources.statement, STATEMENT)
    assert withdrawn_references(db_session, seeded_sources.statement, *PERIOD) == set()


def test_current_records_ignores_the_superseded_version(
    db_session: Session, seeded_sources
) -> None:
    load(db_session, seeded_sources.statement, STATEMENT)
    load(db_session, seeded_sources.statement, CORRECTED)
    current = current_records(db_session, seeded_sources.statement, *PERIOD)
    assert len(current) == CORRECTED_ROWS
    assert WITHDRAWN_REFERENCE not in {r.reference for r in current}


def test_a_correction_is_not_refused_as_a_duplicate(db_session: Session, seeded_sources) -> None:
    """Same source and period, different bytes: a correction, not a resend."""
    load(db_session, seeded_sources.statement, STATEMENT)
    result = load(db_session, seeded_sources.statement, CORRECTED)
    assert result.version_no == 2


def _first_corrected_reference(session: Session, source) -> str:
    """A reference whose amount differs between the two versions."""
    rows = list(
        session.scalars(
            select(Record).where(Record.source_id == source.id).order_by(Record.reference)
        )
    )
    seen: dict[str, Decimal] = {}
    for row in rows:
        if row.reference in seen and seen[row.reference] != row.gross_amount:
            return row.reference
        seen[row.reference] = row.gross_amount
    raise AssertionError("no corrected amount found in the sample data")
