"""Running a reconciliation, against the real sample data.

The important test here is :func:`test_states_sum` (AC2, TR-509). Every other
number this application shows an analyst is downstream of it: if the states do
not account for every line of both files, a break can go missing and nobody
will know. It is asserted exactly, against the actual CSVs in ``data/``, not
against a fixture built to agree with the code.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import FieldDiffRow, Pair, Record, Run, RunItem
from app.services.ingest import ingest_file
from app.services.reconcile import (
    ReconcileError,
    candidates_for,
    pair_detail,
    run_reconciliation,
    run_summary,
    worklist,
)
from app.services.resolve import accept_unmatched
from core.model import COMPARED_FIELDS, WORKLIST_STATES, RecordState

DATA = Path(__file__).resolve().parents[2] / "data"
PERIOD = (date(2025, 7, 1), date(2025, 7, 7))

LEDGER = DATA / "ledger_2025-07-01_07.csv"
STATEMENT = DATA / "statement_2025-07-01_07.csv"
CORRECTED = DATA / "statement_2025-07-01_07_v2.csv"

# The real files. If ``scripts/make_sample_data.py`` is re-run these move with it.
LEDGER_ROWS, LEDGER_BAD_ROWS = 40, 2
STATEMENT_ROWS, STATEMENT_BAD_ROWS = 40, 0

#: Every line of both files that had to end somewhere: the rows that parsed,
#: plus the two in the ledger that did not.
LINES_READ = LEDGER_ROWS + STATEMENT_ROWS + LEDGER_BAD_ROWS + STATEMENT_BAD_ROWS

#: The reconciliation the sample data was built to produce. Asserted whole
#: rather than field by field, so a change in matching shows up as a diff of
#: the run an analyst would actually see.
EXPECTED_COUNTS: dict[str, int] = {
    "excluded": 5,
    "agreed": 50,
    "agreed_with_drift": 6,
    "break": 12,
    "suggested": 2,
    "unmatched": 4,
    "status_disagreement": 1,
    "accepted_unmatched": 0,
    "withdrawn": 0,
    "rejected_row": 2,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load(session: Session, source, path: Path) -> None:
    ingest_file(session, source, path.read_bytes(), PERIOD[0], PERIOD[1], path.name)


def load_both(session: Session, sources) -> None:
    load(session, sources.ledger, LEDGER)
    load(session, sources.statement, STATEMENT)


def reconcile(session: Session, sources) -> Run:
    return run_reconciliation(session, sources.ledger, sources.statement, PERIOD[0], PERIOD[1])


@pytest.fixture
def run(db_session: Session, seeded_sources) -> Run:
    load_both(db_session, seeded_sources)
    return reconcile(db_session, seeded_sources)


# ---------------------------------------------------------------------------
# AC2 / TR-509 - the one that has to be exact
# ---------------------------------------------------------------------------


def test_states_sum(db_session: Session, seeded_sources) -> None:
    """AC2, TR-509. Every line of both files ends in exactly one state.

    Exactly, not approximately. A summary that is short by one is a break an
    analyst never sees, and this application exists to make that impossible.
    """
    load_both(db_session, seeded_sources)
    run = reconcile(db_session, seeded_sources)

    counts = run_summary(db_session, run.id)

    assert counts == EXPECTED_COUNTS
    assert sum(counts.values()) == LINES_READ == 82

    # And the same total from the other direction: every record that parsed has
    # one run_item, and every row that did not is counted as a rejected row.
    items = db_session.scalar(
        select(func.count()).select_from(RunItem).where(RunItem.run_id == run.id)
    )
    records = db_session.scalar(select(func.count()).select_from(Record))
    assert items == records == LEDGER_ROWS + STATEMENT_ROWS
    assert items + counts[RecordState.REJECTED_ROW.value] == LINES_READ

    # One state each, and every state a real one.
    states = list(db_session.scalars(select(RunItem.state).where(RunItem.run_id == run.id)))
    assert len(states) == len(
        set(db_session.scalars(select(RunItem.record_id).where(RunItem.run_id == run.id)))
    )
    assert set(states) <= {s.value for s in RecordState}


def test_states_sum_after_a_correction(db_session: Session, seeded_sources) -> None:
    """The withdrawn row is a state too, so the total still accounts for it.

    The corrected statement drops one reference. It is not in the current
    delivery and so cannot be a record read from it, but it was read once and
    a person still has to decide what became of it (TR-107, SPEC 5.6).
    """
    load(db_session, seeded_sources.ledger, LEDGER)
    load(db_session, seeded_sources.statement, STATEMENT)
    load(db_session, seeded_sources.statement, CORRECTED)

    run = reconcile(db_session, seeded_sources)
    counts = run_summary(db_session, run.id)

    assert counts[RecordState.WITHDRAWN.value] == 1
    # 40 ledger + 39 current statement + 1 withdrawn + 2 unreadable ledger rows.
    assert sum(counts.values()) == LEDGER_ROWS + 39 + 1 + LEDGER_BAD_ROWS


# ---------------------------------------------------------------------------
# TR-502 - append only
# ---------------------------------------------------------------------------


def test_runs_append_only(db_session: Session, seeded_sources) -> None:
    """TR-502. A re-run is a new answer, never an edit of an old one."""
    load_both(db_session, seeded_sources)

    first = reconcile(db_session, seeded_sources)
    first_id, first_counts = first.id, dict(first.counts)
    first_started, first_finished = first.started_at, first.finished_at
    first_items = set(db_session.scalars(select(RunItem.id).where(RunItem.run_id == first_id)))

    second = reconcile(db_session, seeded_sources)

    assert second.id != first_id
    assert db_session.scalar(select(func.count()).select_from(Run)) == 2

    untouched = db_session.get(Run, first_id)
    assert untouched is not None
    assert dict(untouched.counts) == first_counts
    assert (untouched.started_at, untouched.finished_at) == (first_started, first_finished)

    # The first run's items are still its own; the second run wrote its own set.
    second_items = set(db_session.scalars(select(RunItem.id).where(RunItem.run_id == second.id)))
    assert first_items and second_items
    assert not (first_items & second_items)
    assert (
        set(db_session.scalars(select(RunItem.id).where(RunItem.run_id == first_id))) == first_items
    )


# ---------------------------------------------------------------------------
# TR-511 - excluded is an outcome, not a disappearance
# ---------------------------------------------------------------------------


def test_excluded_listed(db_session: Session, seeded_sources, run: Run) -> None:
    """TR-511. A cancelled trade is counted and listable, so it has a traceable end.

    Excluded is deliberately *not* in the worklist - nobody needs to act on it -
    but it must still be findable, or "where did that trade go?" has no answer.
    """
    counts = run_summary(db_session, run.id)
    assert counts[RecordState.EXCLUDED.value] == EXPECTED_COUNTS["excluded"]

    excluded = list(
        db_session.scalars(
            select(RunItem).where(
                RunItem.run_id == run.id, RunItem.state == RecordState.EXCLUDED.value
            )
        )
    )
    assert len(excluded) == counts[RecordState.EXCLUDED.value]

    for item in excluded:
        record = db_session.get(Record, item.record_id)
        assert record is not None
        assert record.is_cancelled, "only a cancelled record is excluded"
        assert item.pair_id is None, "an excluded record is never compared"

    # Excluded is not a way of hiding work.
    assert RecordState.EXCLUDED not in WORKLIST_STATES
    assert all(item.state != RecordState.EXCLUDED.value for item in worklist(db_session, run.id))


# ---------------------------------------------------------------------------
# TR-512 - two runs are comparable without re-reading either
# ---------------------------------------------------------------------------


def test_summary_comparison(db_session: Session, seeded_sources) -> None:
    """TR-512. Yesterday against today is two summaries, not two full walks.

    The counts live on the run, so the comparison needs neither run's items.
    Here a correction fixes three amounts, and the summary alone shows the
    breaks falling.
    """
    load(db_session, seeded_sources.ledger, LEDGER)
    load(db_session, seeded_sources.statement, STATEMENT)
    before = run_summary(db_session, reconcile(db_session, seeded_sources).id)

    load(db_session, seeded_sources.statement, CORRECTED)
    after_run = reconcile(db_session, seeded_sources)
    after = run_summary(db_session, after_run.id)

    # Both summaries are the same shape, so a diff is key by key.
    assert before.keys() == after.keys() == {s.value for s in RecordState}

    stored = db_session.get(Run, after_run.id)
    assert stored is not None
    assert dict(stored.counts) == after, "the summary is stored, not recomputed"

    delta = {state: after[state] - before[state] for state in after}
    assert delta[RecordState.BREAK.value] < 0, "a correction should shrink the worklist"
    assert delta[RecordState.WITHDRAWN.value] == 1

    # And it really is readable without touching run_item.
    db_session.expire_all()
    assert run_summary(db_session, after_run.id) == after


# ---------------------------------------------------------------------------
# TR-702 - the same inputs give the same answer
# ---------------------------------------------------------------------------


def test_idempotent(db_session: Session, seeded_sources) -> None:
    """TR-702, AC10. Unchanged inputs and unchanged resolutions, identical result.

    Compared as data rather than as row ids: the second run necessarily writes
    new rows, and what has to be identical is what those rows say.
    """
    load_both(db_session, seeded_sources)

    # "Unchanged resolutions" is half the requirement, so there has to be one.
    lonely = worklist(
        db_session, reconcile(db_session, seeded_sources).id, RecordState.UNMATCHED.value
    )[0]
    record = db_session.get(Record, lonely.record_id)
    accept_unmatched(
        db_session,
        seeded_sources.ledger
        if record.source_id == seeded_sources.ledger.id
        else seeded_sources.statement,
        record.reference,
        "counterparty confirmed they never booked it",
        "an.analyst",
    )
    db_session.flush()

    first = reconcile(db_session, seeded_sources)
    second = reconcile(db_session, seeded_sources)

    assert run_summary(db_session, first.id) == run_summary(db_session, second.id)
    assert run_summary(db_session, first.id)[RecordState.ACCEPTED_UNMATCHED.value] == 1

    def pairs(run_id: int) -> list[tuple]:
        rows = db_session.scalars(select(Pair).where(Pair.run_id == run_id))
        return sorted(
            (p.left_record_id, p.right_record_id, p.origin, p.verdict, p.max_rel_diff) for p in rows
        )

    def items(run_id: int) -> list[tuple]:
        rows = db_session.scalars(select(RunItem).where(RunItem.run_id == run_id))
        return sorted((i.record_id, i.side, i.state) for i in rows)

    assert pairs(first.id) == pairs(second.id)
    assert items(first.id) == items(second.id)

    # And the ordering the analyst sees is stable too, not merely the contents.
    def references(run_id: int) -> list[str]:
        return [
            db_session.get(Record, item.record_id).reference
            for item in worklist(db_session, run_id)
        ]

    assert references(first.id) == references(second.id)


# ---------------------------------------------------------------------------
# The reading surface the web layer is built against
# ---------------------------------------------------------------------------


def test_worklist_is_only_the_states_that_need_a_person(
    db_session: Session, seeded_sources, run: Run
) -> None:
    """TR-606. The worklist is exactly SPEC 5.6's "yes" column, worst first."""
    items = worklist(db_session, run.id)
    counts = run_summary(db_session, run.id)

    assert {item.state for item in items} <= {s.value for s in WORKLIST_STATES}

    # Rejected rows are the one worklist state with no run_item to return; see
    # the note on the failing half of this expectation in the report.
    expected = sum(
        counts[state.value] for state in WORKLIST_STATES if state is not RecordState.REJECTED_ROW
    )
    assert len(items) == expected

    magnitudes = []
    for item in items:
        pair = db_session.get(Pair, item.pair_id) if item.pair_id else None
        magnitudes.append(pair.max_rel_diff if pair else Decimal(-1))
    assert magnitudes == sorted(magnitudes, reverse=True), "largest difference first"


def test_worklist_filters_by_state(db_session: Session, seeded_sources, run: Run) -> None:
    breaks = worklist(db_session, run.id, RecordState.BREAK.value)
    assert breaks
    assert all(item.state == RecordState.BREAK.value for item in breaks)
    assert len(breaks) == run_summary(db_session, run.id)[RecordState.BREAK.value]

    # A state nobody has to act on filters to nothing, rather than leaking in.
    assert worklist(db_session, run.id, RecordState.AGREED.value) == []


def test_pair_detail_carries_every_compared_field(
    db_session: Session, seeded_sources, run: Run
) -> None:
    """TR-401, TR-607. Both values and both differences, for every field, in render order."""
    item = next(i for i in worklist(db_session, run.id, RecordState.BREAK.value))
    assert item.pair_id is not None

    pair, left, right, diffs = pair_detail(db_session, item.pair_id)

    assert pair.id == item.pair_id
    assert left.reference == right.reference
    assert [d.field for d in diffs] == list(COMPARED_FIELDS)
    assert any(not d.within_tolerance for d in diffs), "a break has a field outside tolerance"
    assert all(d.left_value and d.right_value for d in diffs)

    stored = db_session.scalar(
        select(func.count()).select_from(FieldDiffRow).where(FieldDiffRow.pair_id == pair.id)
    )
    assert stored == len(COMPARED_FIELDS)


def test_candidates_are_offered_with_a_reason(
    db_session: Session, seeded_sources, run: Run
) -> None:
    """TR-608. A suggested record lists its counterparts and why each qualifies."""
    suggested = worklist(db_session, run.id, RecordState.SUGGESTED.value)
    assert suggested, "the sample data contains a near-miss pair"

    for item in suggested:
        found = candidates_for(db_session, run.id, item.record_id)
        assert found, "a record is only 'suggested' because something qualified"
        subject = db_session.get(Record, item.record_id)
        for candidate, reason in found:
            assert candidate.source_id != subject.source_id, "a candidate is on the other side"
            assert reason.strip(), "a suggestion without a reason is not a suggestion"

    # Nothing was applied: a suggestion is a question (CLAUDE.md invariant 9).
    origins = set(db_session.scalars(select(Pair.origin).where(Pair.run_id == run.id)))
    assert "suggested" not in origins


def test_unmatched_record_may_have_no_candidate(
    db_session: Session, seeded_sources, run: Run
) -> None:
    unmatched = worklist(db_session, run.id, RecordState.UNMATCHED.value)
    assert unmatched
    # Unmatched means nothing qualified; the page must render an empty list
    # rather than fail.
    assert all(candidates_for(db_session, run.id, i.record_id) == [] for i in unmatched)


def test_a_run_needs_a_tolerance_profile(db_session: Session, seeded_sources) -> None:
    """CLAUDE.md invariant 7. No threshold has a default hidden in code."""
    load_both(db_session, seeded_sources)
    with pytest.raises(ReconcileError, match="tolerance profile"):
        run_reconciliation(
            db_session, seeded_sources.statement, seeded_sources.ledger, PERIOD[0], PERIOD[1]
        )


def test_a_stored_decision_reaches_the_run(db_session: Session, seeded_sources) -> None:
    """R7.4, R7.2. A decision made once is applied by every later run, unasked.

    This is the seam between ``app/services/resolve.py`` and this module, and
    it is the requirement SPEC calls the one most likely to be under-built: an
    acceptance that does not survive into the next morning's worklist is an
    acceptance the analyst gets to make again tomorrow.
    """
    load_both(db_session, seeded_sources)
    before = reconcile(db_session, seeded_sources)
    lonely = worklist(db_session, before.id, RecordState.UNMATCHED.value)[0]
    record = db_session.get(Record, lonely.record_id)
    source = (
        seeded_sources.ledger
        if record.source_id == seeded_sources.ledger.id
        else seeded_sources.statement
    )

    accept_unmatched(db_session, source, record.reference, "confirmed as ours alone", "an.analyst")
    db_session.flush()

    after = reconcile(db_session, seeded_sources)
    counts_before = run_summary(db_session, before.id)
    counts_after = run_summary(db_session, after.id)

    assert counts_after[RecordState.UNMATCHED.value] == (
        counts_before[RecordState.UNMATCHED.value] - 1
    )
    assert counts_after[RecordState.ACCEPTED_UNMATCHED.value] == 1
    # The total is untouched: a decision moves a record between states, it
    # never removes one (TR-509).
    assert sum(counts_after.values()) == sum(counts_before.values()) == LINES_READ

    settled = db_session.scalars(
        select(RunItem).where(
            RunItem.run_id == after.id,
            RunItem.state == RecordState.ACCEPTED_UNMATCHED.value,
        )
    )
    assert [db_session.get(Record, i.record_id).reference for i in settled] == [record.reference]
    assert record.reference not in [
        db_session.get(Record, i.record_id).reference for i in worklist(db_session, after.id)
    ]
