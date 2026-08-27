"""TR-706. Ingest and run leave a machine-readable trace.

These are the two events that change what an analyst sees the next morning, so
each has to answer, without anyone opening the database: which source, which
period, which version of the file, and what came out. A log line that says
"reconciliation finished" answers none of them.

The counts are asserted against the same numbers the caller was handed, because
a log that disagrees with the result it describes is worse than no log.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.observability import (
    INGEST_EVENT,
    LOGGER_NAME,
    RUN_EVENT,
    JsonFormatter,
    configure_logging,
    get_logger,
)
from app.services.ingest import ingest_file
from app.services.reconcile import run_reconciliation, run_summary

DATA = Path(__file__).resolve().parents[2] / "data"
PERIOD = (date(2025, 7, 1), date(2025, 7, 7))
LEDGER = DATA / "ledger_2025-07-01_07.csv"
STATEMENT = DATA / "statement_2025-07-01_07.csv"
CORRECTED = DATA / "statement_2025-07-01_07_v2.csv"


def _events(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = get_logger()
    previous = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


# ---------------------------------------------------------------------------
# TR-706
# ---------------------------------------------------------------------------


def test_ingest_emits_its_counts(
    db_session: Session, seeded_sources, log_stream: io.StringIO
) -> None:
    """TR-706. The ingest event names the source, the period, the version, and the cost."""
    result = ingest_file(
        db_session, seeded_sources.ledger, LEDGER.read_bytes(), *PERIOD, LEDGER.name
    )

    events = [e for e in _events(log_stream) if e["event"] == INGEST_EVENT]
    assert len(events) == 1
    event = events[0]

    assert event["source"] == "ledger"
    assert event["period_start"] == str(PERIOD[0])
    assert event["period_end"] == str(PERIOD[1])
    assert event["version"] == result.version_no == 1
    assert event["batch_id"] == result.batch_id
    assert event["counts"] == {
        "accepted_rows": result.accepted_rows,
        "rejected_rows": result.rejected_rows,
        "withdrawn": 0,
    }
    # The two unreadable ledger rows are in the trace, not only in the database.
    assert event["counts"]["rejected_rows"] == 2
    assert event["level"] == "INFO"
    assert event["logger"] == LOGGER_NAME


def test_a_correction_says_it_superseded_something(
    db_session: Session, seeded_sources, log_stream: io.StringIO
) -> None:
    """A version-2 delivery is visible as a version-2 delivery, with its withdrawal."""
    ingest_file(
        db_session, seeded_sources.statement, STATEMENT.read_bytes(), *PERIOD, STATEMENT.name
    )
    second = ingest_file(
        db_session, seeded_sources.statement, CORRECTED.read_bytes(), *PERIOD, CORRECTED.name
    )

    events = [e for e in _events(log_stream) if e["event"] == INGEST_EVENT]
    assert [e["version"] for e in events] == [1, 2]
    assert events[1]["superseded_batch_id"] == second.superseded_batch_id
    assert events[1]["counts"]["withdrawn"] == 1


def test_a_refused_file_leaves_no_accepted_event(
    db_session: Session, seeded_sources, log_stream: io.StringIO
) -> None:
    """The event says a file was accepted, so it must not fire when one was not."""
    from app.services.ingest import DuplicateFileError

    ingest_file(db_session, seeded_sources.ledger, LEDGER.read_bytes(), *PERIOD, LEDGER.name)
    with pytest.raises(DuplicateFileError):
        ingest_file(db_session, seeded_sources.ledger, LEDGER.read_bytes(), *PERIOD, "resent.csv")

    assert len([e for e in _events(log_stream) if e["event"] == INGEST_EVENT]) == 1


def test_run_emits_its_state_counts(
    db_session: Session, seeded_sources, log_stream: io.StringIO
) -> None:
    """TR-706. The run event carries the summary itself, so the log and the database agree."""
    ingest_file(db_session, seeded_sources.ledger, LEDGER.read_bytes(), *PERIOD, LEDGER.name)
    ingest_file(
        db_session, seeded_sources.statement, STATEMENT.read_bytes(), *PERIOD, STATEMENT.name
    )
    run = run_reconciliation(db_session, seeded_sources.ledger, seeded_sources.statement, *PERIOD)

    events = [e for e in _events(log_stream) if e["event"] == RUN_EVENT]
    assert len(events) == 1
    event = events[0]

    assert event["run_id"] == run.id
    assert event["source"] == "ledger<->statement"
    assert event["period_start"] == str(PERIOD[0])
    assert event["period_end"] == str(PERIOD[1])
    # A run has no version of its own; it carries the version of each file read.
    assert event["version"] == {"ledger": 1, "statement": 1}
    assert event["counts"] == run_summary(db_session, run.id)
    assert event["records_read"] == 80
    assert sum(event["counts"].values()) == 82


def test_both_events_are_emitted_by_one_pass(
    db_session: Session, seeded_sources, log_stream: io.StringIO
) -> None:
    """The audit trail for a morning is two ingests and a run, in that order."""
    ingest_file(db_session, seeded_sources.ledger, LEDGER.read_bytes(), *PERIOD, LEDGER.name)
    ingest_file(
        db_session, seeded_sources.statement, STATEMENT.read_bytes(), *PERIOD, STATEMENT.name
    )
    run_reconciliation(db_session, seeded_sources.ledger, seeded_sources.statement, *PERIOD)

    events = _events(log_stream)
    assert [e["event"] for e in events] == [INGEST_EVENT, INGEST_EVENT, RUN_EVENT]
    for event in events:
        assert set(event) >= {"ts", "level", "logger", "event", "source", "counts"}
        assert event["counts"], "an event without counts answers nothing"


# ---------------------------------------------------------------------------
# The formatter itself
# ---------------------------------------------------------------------------


def test_every_line_is_one_json_object(log_stream: io.StringIO) -> None:
    """A log a host cannot parse is a log nobody reads."""
    from decimal import Decimal

    get_logger().info("probe", extra={"amount": Decimal("34170.00"), "nested": {"n": 1}})
    lines = log_stream.getvalue().splitlines()
    assert len(lines) == 1

    payload = json.loads(lines[0])
    # Exact string, never a float: a log line is evidence (CLAUDE.md invariant 1).
    assert payload["amount"] == "34170.00"
    assert payload["nested"] == {"n": 1}


def test_configure_logging_is_idempotent() -> None:
    """Called from startup, and startup happens more than once in a test process."""
    logger = get_logger()
    before = list(logger.handlers)
    try:
        configure_logging("INFO", stream=io.StringIO())
        added = len(logger.handlers)
        configure_logging("INFO", stream=io.StringIO())
        assert len(logger.handlers) == added
    finally:
        logger.handlers = before
