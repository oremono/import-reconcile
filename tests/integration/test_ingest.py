"""Loading files, against the real sample data.

Every number asserted here comes from the actual files in ``data/`` - forty
rows a side, two deliberately malformed rows in the ledger. Synthetic fixtures
would pass while the real thing failed, which is the failure mode these tests
exist to prevent.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import FileBatch, Record, RejectedRow
from app.services.ingest import (
    DuplicateFileError,
    MalformedFileError,
    OverlappingPeriodError,
    content_digest,
    ingest_file,
)

DATA = Path(__file__).resolve().parents[2] / "data"
PERIOD = (date(2025, 7, 1), date(2025, 7, 7))

LEDGER = DATA / "ledger_2025-07-01_07.csv"
STATEMENT = DATA / "statement_2025-07-01_07.csv"
RESEND = DATA / "statement_2025-07-01_07_resend.csv"

# The real files. If the sample data is regenerated these must move with it.
LEDGER_ROWS = 40
LEDGER_BAD_ROWS = 2
STATEMENT_ROWS = 40


def load(session: Session, source, path: Path, period=PERIOD):
    return ingest_file(session, source, path.read_bytes(), period[0], period[1], path.name)


# ---------------------------------------------------------------------------
# The ordinary case
# ---------------------------------------------------------------------------


def test_ledger_loads_with_its_bad_rows_isolated(db_session: Session, seeded_sources) -> None:
    result = load(db_session, seeded_sources.ledger, LEDGER)
    assert result.accepted_rows == LEDGER_ROWS
    assert result.rejected_rows == LEDGER_BAD_ROWS
    assert result.version_no == 1
    assert not result.is_correction


def test_bad_rows_isolated(db_session: Session, seeded_sources) -> None:
    """TR-106. One unreadable row must never cost a file its other thirty-nine."""
    load(db_session, seeded_sources.ledger, LEDGER)

    stored = db_session.scalar(select(func.count()).select_from(Record))
    rejected = list(db_session.scalars(select(RejectedRow)))

    assert stored == LEDGER_ROWS
    assert len(rejected) == LEDGER_BAD_ROWS
    reasons = " ".join(r.reason for r in rejected).lower()
    assert "date" in reasons or "timestamp" in reasons
    assert all(r.raw for r in rejected), "a rejected row must keep its original content"
    assert all(r.row_no > 1 for r in rejected), "row numbers are the ones a person sees"


def test_header_validation(db_session: Session, seeded_sources) -> None:
    """TR-101. A file missing a mapped column is refused before any row loads."""
    mangled = LEDGER.read_bytes().replace(b"gross_amount", b"gross_amt", 1)
    with pytest.raises(MalformedFileError):
        ingest_file(db_session, seeded_sources.ledger, mangled, PERIOD[0], PERIOD[1], "mangled.csv")
    assert db_session.scalar(select(func.count()).select_from(Record)) == 0


def test_a_file_that_is_not_csv_is_refused(db_session: Session, seeded_sources) -> None:
    with pytest.raises(MalformedFileError):
        ingest_file(db_session, seeded_sources.ledger, b"", PERIOD[0], PERIOD[1], "empty.csv")


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------


def test_identical_resend_refused(db_session: Session, seeded_sources) -> None:
    """TR-102, AC7. Double-counting a statement is worse than any error message."""
    load(db_session, seeded_sources.statement, STATEMENT)
    before = db_session.scalar(select(func.count()).select_from(Record))

    with pytest.raises(DuplicateFileError) as caught:
        load(db_session, seeded_sources.statement, RESEND)

    assert "Already accepted" in str(caught.value)
    assert db_session.scalar(select(func.count()).select_from(Record)) == before
    assert db_session.scalar(select(func.count()).select_from(FileBatch)) == 1


def test_the_digest_is_of_contents_not_of_the_filename(db_session: Session, seeded_sources) -> None:
    """D6. Filenames are unreliable and often carry the send time."""
    assert content_digest(STATEMENT.read_bytes()) == content_digest(RESEND.read_bytes())
    assert STATEMENT.name != RESEND.name


def test_the_same_file_from_a_different_source_is_not_a_duplicate(
    db_session: Session, seeded_sources
) -> None:
    """The constraint is per source, not global.

    Two counterparties on the same vendor platform send the same layout, and
    could plausibly send byte-identical files. Refusing the second because the
    first arrived would lose a whole counterparty's data.
    """
    from app.db.models import Source

    twin = Source(
        code="statement_two",
        name="Second counterparty, same vendor platform",
        format_config=seeded_sources.statement.format_config,
    )
    db_session.add(twin)
    db_session.flush()

    load(db_session, seeded_sources.statement, STATEMENT)
    load(db_session, twin, STATEMENT)  # same bytes, different source
    assert db_session.scalar(select(func.count()).select_from(FileBatch)) == 2


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------


def test_partial_overlap_refused(db_session: Session, seeded_sources) -> None:
    """TR-104, D21. Exact restatement or no overlap. Nothing in between."""
    load(db_session, seeded_sources.statement, STATEMENT)
    with pytest.raises(OverlappingPeriodError) as caught:
        ingest_file(
            db_session,
            seeded_sources.statement,
            LEDGER.read_bytes(),
            date(2025, 7, 5),
            date(2025, 7, 10),
            "overlapping.csv",
        )
    assert "overlaps" in str(caught.value)
    assert db_session.scalar(select(func.count()).select_from(FileBatch)) == 1


def test_a_neighbouring_period_is_accepted(db_session: Session, seeded_sources) -> None:
    """Refusing an overlap must not refuse the following week.

    Next week's file is in the same format but is different data, so it differs
    in content as well as in period.
    """
    load(db_session, seeded_sources.statement, STATEMENT)
    next_week = STATEMENT.read_bytes().replace(b"T-10", b"T-20")
    ingest_file(
        db_session,
        seeded_sources.statement,
        next_week,
        date(2025, 7, 8),
        date(2025, 7, 14),
        "next_week.csv",
    )
    assert db_session.scalar(select(func.count()).select_from(FileBatch)) == 2


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


def test_rollback_on_failure(db_session: Session, seeded_sources) -> None:
    """TR-108. A file is fully accepted or leaves no trace, batch row included."""
    broken = seeded_sources.statement
    broken.format_config = {**broken.format_config, "timezone": "Mars/Olympus"}
    db_session.flush()

    with pytest.raises(Exception):
        load(db_session, broken, STATEMENT)

    db_session.rollback()
    assert db_session.scalar(select(func.count()).select_from(FileBatch)) == 0
    assert db_session.scalar(select(func.count()).select_from(Record)) == 0


def test_records_keep_their_original_row(db_session: Session, seeded_sources) -> None:
    """`raw` is what makes "what did the file actually say?" answerable."""
    load(db_session, seeded_sources.statement, STATEMENT)
    record = db_session.scalars(select(Record)).first()
    assert record is not None
    assert record.raw
    assert record.occurred_at.tzinfo is not None


def test_amounts_survive_the_round_trip_exactly(db_session: Session, seeded_sources) -> None:
    from decimal import Decimal

    load(db_session, seeded_sources.ledger, LEDGER)
    first = db_session.scalar(select(Record).where(Record.reference == "T-1001"))
    assert first is not None
    assert first.gross_amount == Decimal("4640.00")
    assert first.quantity * first.unit_price == first.gross_amount
