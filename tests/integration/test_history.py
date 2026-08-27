"""Value history: what did this row say before, and which file said it (TR-510).

Records are immutable, so the history is not a feature -- it is already sitting
in the table. Each version of a transaction is its own row under its own batch,
and the whole story of a reference is one indexed join away.

That "one" is the requirement, and it is asserted rather than assumed: the
queries the session issues are counted while ``record_history`` runs. Walking
the batches and fetching each version in turn would produce the same list and
would pass every other test in this file, then become a page that issues a query
per correction (R8.2, R8.3, AC8).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.db.models import Source
from app.services.ingest import ingest_file
from app.services.resolve import ResolutionError, record_history

DATA = Path(__file__).resolve().parents[2] / "data"
PERIOD = (date(2025, 7, 1), date(2025, 7, 7))

LEDGER_FILE = DATA / "ledger_2025-07-01_07.csv"
STATEMENT_FILE = DATA / "statement_2025-07-01_07.csv"
CORRECTED_FILE = DATA / "statement_2025-07-01_07_v2.csv"

# The brief's own worked example: 34,170.00 restated as 34,000.00.
CORRECTED_REFERENCE = "T-1010"
ORIGINAL_AMOUNT = Decimal("34170.00")
RESTATED_AMOUNT = Decimal("34000.00")

# Dropped by the correction. Its rows are not deleted, so its history answers.
WITHDRAWN_REFERENCE = "C-9002"


def load(session: Session, source: Source, path: Path, content: bytes | None = None):
    body = path.read_bytes() if content is None else content
    return ingest_file(session, source, body, PERIOD[0], PERIOD[1], path.name)


def restored_correction() -> bytes:
    """A third version: the corrected file with the dropped row put back.

    Two versions cannot tell "one query" apart from "one query per version".
    Three can.
    """
    kept = [
        line
        for line in STATEMENT_FILE.read_bytes().splitlines()
        if line.startswith(WITHDRAWN_REFERENCE.encode())
    ]
    assert kept, "the sample statement is meant to contain the row the correction drops"
    return CORRECTED_FILE.read_bytes().rstrip(b"\n") + b"\n" + kept[0] + b"\n"


@contextmanager
def counting_queries(session: Session) -> Iterator[list[str]]:
    """Every SQL statement the session issues while the block runs."""
    statements: list[str] = []
    bind = session.get_bind()
    engine = getattr(bind, "engine", bind)

    def record(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def selects(statements: list[str]) -> list[str]:
    return [s for s in statements if s.strip().upper().startswith("SELECT")]


# ---------------------------------------------------------------------------
# The requirement
# ---------------------------------------------------------------------------


def test_history_is_one_query_ordered_by_version(db_session: Session, seeded_sources) -> None:
    """TR-510, R8.2, R8.3, AC8. Every version of a record, oldest first, in one query.

    Three versions of the same period, so a per-version loop would show up as
    three queries rather than hiding behind a list that happens to be right.
    """
    load(db_session, seeded_sources.statement, STATEMENT_FILE)
    load(db_session, seeded_sources.statement, CORRECTED_FILE)
    load(db_session, seeded_sources.statement, CORRECTED_FILE, content=restored_correction())
    db_session.flush()

    with counting_queries(db_session) as statements:
        history = record_history(db_session, seeded_sources.statement, CORRECTED_REFERENCE)

    assert len(selects(statements)) == 1, (
        "history is one join, not a loop over batches: "
        f"{len(selects(statements))} queries were issued"
    )

    assert [batch.version_no for batch, _ in history] == [1, 2, 3]
    assert [record.gross_amount for _, record in history] == [
        ORIGINAL_AMOUNT,
        RESTATED_AMOUNT,
        RESTATED_AMOUNT,
    ]


def test_history_names_the_file_each_version_came_from(db_session: Session, seeded_sources) -> None:
    """R8.3. "Which file said this, and when did we accept it?" is the other half."""
    first = load(db_session, seeded_sources.statement, STATEMENT_FILE)
    second = load(db_session, seeded_sources.statement, CORRECTED_FILE)

    history = record_history(db_session, seeded_sources.statement, CORRECTED_REFERENCE)
    batches = [batch for batch, _ in history]

    assert [batch.id for batch in batches] == [first.batch_id, second.batch_id]
    assert [batch.filename for batch in batches] == [STATEMENT_FILE.name, CORRECTED_FILE.name]
    for batch in batches:
        assert batch.accepted_at is not None
        assert batch.accepted_at.tzinfo is not None, "TR-506: stored timestamps are aware UTC"

    # The older row is still the one the older batch carries. Nothing was
    # overwritten, so both answers survive (CLAUDE.md invariant 3).
    assert history[0][1].batch_id == first.batch_id
    assert history[1][1].batch_id == second.batch_id


def test_history_of_an_unchanged_record_is_still_a_full_history(
    db_session: Session, seeded_sources
) -> None:
    """A correction restates the whole period, so every row gets a new version.

    Which is exactly why a resolution cannot key on a row id: even a record
    nobody corrected is a different row afterwards (TR-508).
    """
    load(db_session, seeded_sources.statement, STATEMENT_FILE)
    load(db_session, seeded_sources.statement, CORRECTED_FILE)

    history = record_history(db_session, seeded_sources.statement, "T-1001")
    assert [batch.version_no for batch, _ in history] == [1, 2]
    ids = [record.id for _, record in history]
    assert ids[0] != ids[1]
    assert history[0][1].gross_amount == history[1][1].gross_amount


def test_a_withdrawn_row_still_has_a_history(db_session: Session, seeded_sources) -> None:
    """TR-708, D8. The correction dropped it; nothing deleted it.

    "It used to be here and now it is not" is a finding, and a finding nobody
    can look at is not much of one.
    """
    load(db_session, seeded_sources.statement, STATEMENT_FILE)
    load(db_session, seeded_sources.statement, CORRECTED_FILE)

    history = record_history(db_session, seeded_sources.statement, WITHDRAWN_REFERENCE)
    assert [batch.version_no for batch, _ in history] == [1]
    assert history[0][1].gross_amount == Decimal("5103.00")


def test_history_is_scoped_to_one_source(db_session: Session, seeded_sources) -> None:
    """Both sides use the same reference for the same trade; they are not the same record."""
    load(db_session, seeded_sources.ledger, LEDGER_FILE)
    load(db_session, seeded_sources.statement, STATEMENT_FILE)

    ours = record_history(db_session, seeded_sources.ledger, CORRECTED_REFERENCE)
    theirs = record_history(db_session, seeded_sources.statement, CORRECTED_REFERENCE)

    assert {record.source_id for _, record in ours} == {seeded_sources.ledger.id}
    assert {record.source_id for _, record in theirs} == {seeded_sources.statement.id}
    assert ours[0][1].gross_amount == RESTATED_AMOUNT
    assert theirs[0][1].gross_amount == ORIGINAL_AMOUNT


def test_history_of_an_unknown_reference_is_empty_not_an_error(
    db_session: Session, seeded_sources
) -> None:
    load(db_session, seeded_sources.statement, STATEMENT_FILE)
    assert record_history(db_session, seeded_sources.statement, "T-0000") == []


def test_history_needs_a_reference(db_session: Session, seeded_sources) -> None:
    with pytest.raises(ResolutionError, match="reference"):
        record_history(db_session, seeded_sources.statement, "  ")
