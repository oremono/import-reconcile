"""Durable decisions: the requirement most likely to be under-built.

SPEC section 5.7 states it as a hard rule -- *a decision made by a person must
never need to be made twice* -- and everything in this file is one way of
probing whether that is true or merely claimed.

The sharpest of them is :func:`test_survives_correction`. A correction writes
**new** ``record`` rows for the same transactions, so every resolution stored
against a row id detaches silently the morning a counterparty resends a file.
That test exists to fail loudly if anyone ever "simplifies" the resolution table
into a foreign key to ``record.id`` (TR-508, DD-5).

Most of these drive the durability path directly: ingest the real sample files,
record a decision through ``app.services.resolve``, then match the *current*
records with the stored resolutions carried forward. That is the narrowest
statement of what a resolution has to survive, and it fails for one reason only.

The last section makes the same claims again through ``run_reconciliation`` and
the worklist, because AC9 and AC10 are written about what the analyst sees on a
Tuesday morning -- so a decision that survives in ``core.match`` but not the trip
through the database is caught here too rather than in a demo.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import FileBatch, Pair, Record, Resolution, RunItem, Source
from app.services.ingest import current_records, ingest_file
from app.services.resolve import (
    ResolutionError,
    accept_unmatched,
    manual_match,
    prior_resolutions,
    reject_suggestion,
    resolution_for,
    revoke,
)
from app.sources import DEFAULT_TOLERANCES
from core.compare import compare
from core.match import match
from core.model import (
    Comparison,
    MatchResult,
    NormalizedRecord,
    PairOrigin,
    RecordState,
    RecordStatus,
    ResolutionKind,
    Side,
    Verdict,
)
from core.tolerance import tolerances_from_config

# The run-level assertions below go through the real reconciler rather than
# through ``core.match`` directly, because AC9 and AC10 are written about what
# the analyst sees on the worklist. That module is Agent G's; until it lands
# those tests skip rather than grow a second implementation of it here.
try:
    from app.services.reconcile import run_reconciliation, worklist
except ImportError:  # pragma: no cover - depends on merge order
    run_reconciliation = None
    worklist = None

needs_the_reconciler = pytest.mark.skipif(
    run_reconciliation is None,
    reason="app/services/reconcile.py has not landed; the run-level assertions need it",
)

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
PERIOD = (date(2025, 7, 1), date(2025, 7, 7))

LEDGER_FILE = DATA / "ledger_2025-07-01_07.csv"
STATEMENT_FILE = DATA / "statement_2025-07-01_07.csv"
CORRECTED_FILE = DATA / "statement_2025-07-01_07_v2.csv"

TOLERANCES = tolerances_from_config(DEFAULT_TOLERANCES)

# Ledger T-1022 and statement C-91023 are one trade under two references:
# BTC-USD, SELL, 0.40 at 62,100.00, booked twelve minutes apart. Nothing pairs
# them automatically -- the references differ -- so a person has to, which makes
# them the natural subject of every test about a decision surviving.
PAIRED_LEFT = "T-1022"
PAIRED_RIGHT = "C-91023"

# Ledger T-1012 is on our side and on no version of the statement.
LONELY_LEFT = "T-1012"
# Statement C-9001 is on theirs and on no version of the ledger.
LONELY_RIGHT = "C-9001"

# The brief's own worked example. The statement first says 34,170.00 against our
# 34,000.00 -- a break -- and the correction restates it as 34,000.00.
CORRECTED_REFERENCE = "T-1010"

AUTHOR = "r.kohale"
REASON = "counterparty books this trade under their own reference C-91023"


# ---------------------------------------------------------------------------
# Driving the durability path
# ---------------------------------------------------------------------------


def load(session: Session, source: Source, path: Path, content: bytes | None = None):
    """Ingest one delivery for the standard period."""
    body = path.read_bytes() if content is None else content
    return ingest_file(session, source, body, PERIOD[0], PERIOD[1], path.name)


def load_both(session: Session, sources) -> None:
    load(session, sources.ledger, LEDGER_FILE)
    load(session, sources.statement, STATEMENT_FILE)


def as_normalized(row: Record, source_code: str) -> NormalizedRecord:
    """One stored row in the vocabulary ``core`` speaks.

    Deliberately the only place these tests touch ``record.id`` -- and it does
    not: matching, and therefore every resolution, is expressed in
    ``(source_code, reference)`` alone.
    """
    return NormalizedRecord(
        source_code=source_code,
        reference=row.reference,
        occurred_at=row.occurred_at,
        instrument=row.instrument,
        side=Side(row.side),
        quantity=row.quantity,
        unit_price=row.unit_price,
        gross_amount=row.gross_amount,
        status=RecordStatus(row.status),
        row_no=row.row_no,
        raw=row.raw,
    )


def run(session: Session, sources) -> MatchResult:
    """Match the *current* records of both sources, carrying decisions forward.

    Reads through ``current_records``, so a correction is visible here the
    moment it lands, and through ``prior_resolutions``, so a revoked decision
    stops applying the moment it is revoked.
    """
    left = [
        as_normalized(row, sources.ledger.code)
        for row in current_records(session, sources.ledger, *PERIOD)
    ]
    right = [
        as_normalized(row, sources.statement.code)
        for row in current_records(session, sources.statement, *PERIOD)
    ]
    return match(
        left, right, TOLERANCES, prior_resolutions(session, sources.ledger, sources.statement)
    )


def pair_for(result: MatchResult, left_reference: str):
    for pair in result.pairs:
        if pair.left.reference == left_reference:
            return pair
    return None


def suggestion_for(result: MatchResult, left_reference: str, right_reference: str):
    for suggestion in result.suggestions:
        if (suggestion.left.reference, suggestion.right.reference) == (
            left_reference,
            right_reference,
        ):
            return suggestion
    return None


def compared(pair) -> Comparison:
    """The same comparison an automatic pair gets. No side is authoritative (TR-316)."""
    return compare(pair.left, pair.right, TOLERANCES)


def record_ids(session: Session, source: Source) -> set[int]:
    """The row ids under the source's *current* batch."""
    return {row.id for row in current_records(session, source, *PERIOD)}


def references(records) -> set[str]:
    return {record.reference for record in records}


# ---------------------------------------------------------------------------
# TR-315 -- confirming a suggestion
# ---------------------------------------------------------------------------


def test_confirmed_suggestion_persists(db_session: Session, seeded_sources) -> None:
    """TR-315, R4.5. A confirmed suggestion is a manual match and nothing else.

    The point of the requirement is that confirming a suggestion creates no
    second-class kind of decision: it carries forward exactly like a pairing a
    person typed in from scratch. So this test makes both -- one confirmed from
    a suggestion, one never suggested at all -- and asserts they are
    indistinguishable afterwards.
    """
    load_both(db_session, seeded_sources)

    offered = suggestion_for(run(db_session, seeded_sources), PAIRED_LEFT, PAIRED_RIGHT)
    assert offered is not None, "the sample data is meant to offer this pair as a suggestion"
    assert offered.rank == 1

    # A suggestion is a question, never an assertion: until it is confirmed it
    # creates no pair (TR-309, D4).
    assert pair_for(run(db_session, seeded_sources), PAIRED_LEFT) is None

    confirmed = manual_match(
        db_session,
        seeded_sources.ledger,
        offered.left.reference,
        seeded_sources.statement,
        offered.right.reference,
        REASON,
        AUTHOR,
    )
    from_scratch = manual_match(
        db_session,
        seeded_sources.ledger,
        LONELY_LEFT,
        seeded_sources.statement,
        LONELY_RIGHT,
        "netted against their consolidated booking",
        AUTHOR,
    )

    result = run(db_session, seeded_sources)

    for reference in (PAIRED_LEFT, LONELY_LEFT):
        pair = pair_for(result, reference)
        assert pair is not None, f"{reference} should be paired by its stored resolution"
        assert pair.origin is PairOrigin.MANUAL

    # Neither record is offered again, and neither is reported as unmatched.
    assert suggestion_for(result, PAIRED_LEFT, PAIRED_RIGHT) is None
    assert not references(result.unmatched_left) & {PAIRED_LEFT, LONELY_LEFT}
    assert not references(result.unmatched_right) & {PAIRED_RIGHT, LONELY_RIGHT}

    # The two decisions are the same kind of thing. Nothing records that one of
    # them began life as a suggestion, because nothing should.
    assert confirmed.kind == from_scratch.kind == str(ResolutionKind.MANUAL_MATCH)
    assert confirmed.reason == REASON
    assert confirmed.author == AUTHOR
    assert confirmed.revoked_at is None


def test_a_manual_pair_is_compared_like_any_other(db_session: Session, seeded_sources) -> None:
    """R7.5, TR-316. Pairing asserts identity, not agreement.

    T-1022 and C-91023 are twelve minutes apart against a five-minute tolerance,
    so confirming them produces a break -- which is correct and is the whole
    point. A manual pair that could never be a break would mean the analyst had
    been given a way to hide a problem by resolving it.
    """
    load_both(db_session, seeded_sources)
    manual_match(
        db_session,
        seeded_sources.ledger,
        PAIRED_LEFT,
        seeded_sources.statement,
        PAIRED_RIGHT,
        REASON,
        AUTHOR,
    )

    pair = pair_for(run(db_session, seeded_sources), PAIRED_LEFT)
    assert pair is not None
    comparison = compared(pair)
    assert comparison.verdict is Verdict.BREAK
    assert [d.field for d in comparison.out_of_tolerance] == ["occurred_at"]


# ---------------------------------------------------------------------------
# TR-317, TR-508 -- the sharpest test in the project
# ---------------------------------------------------------------------------


def test_survives_correction(db_session: Session, seeded_sources) -> None:
    """TR-317, TR-508, R7.6, D9, AC9. A decision outlives the rows it was made about.

    The sequence is the one that breaks a naive implementation:

    1. Both files load and a run is made.
    2. The analyst pairs two records by hand, with a reason and an author.
    3. The counterparty sends a correction. Records are immutable, so this
       writes a **complete set of new record rows** under a new batch -- new
       primary keys for every reference in the file, including the one the
       analyst just resolved.
    4. The next run reads the corrected rows.
    5. The pair still stands, the reason and the author are still there, and the
       comparison is against the new values.

    Step 5 is the assertion that matters. **Had the resolution been keyed on
    ``record.id``, it would now point at a superseded row and the pair would
    quietly cease to exist** -- and the analyst would be asked to make the same
    decision again, on the morning they can least afford it. The test asserts
    the old row id is gone from the current batch precisely so that a reader can
    see what is being defended.
    """
    load_both(db_session, seeded_sources)

    decision = manual_match(
        db_session,
        seeded_sources.ledger,
        PAIRED_LEFT,
        seeded_sources.statement,
        PAIRED_RIGHT,
        REASON,
        AUTHOR,
    )
    before = run(db_session, seeded_sources)
    assert pair_for(before, PAIRED_LEFT) is not None

    statement_rows_before = record_ids(db_session, seeded_sources.statement)
    resolved_row_before = db_session.scalar(
        select(Record).where(
            Record.source_id == seeded_sources.statement.id,
            Record.reference == PAIRED_RIGHT,
        )
    )
    assert resolved_row_before is not None
    corrected_before = pair_for(before, CORRECTED_REFERENCE)
    assert corrected_before is not None
    assert compared(corrected_before).verdict is Verdict.BREAK

    # --- the correction ----------------------------------------------------
    correction = load(db_session, seeded_sources.statement, CORRECTED_FILE)
    assert correction.is_correction
    assert correction.version_no == 2

    statement_rows_after = record_ids(db_session, seeded_sources.statement)
    assert not statement_rows_before & statement_rows_after, (
        "a correction writes an entirely new set of record rows; that is the "
        "condition every resolution has to survive"
    )
    assert resolved_row_before.id not in statement_rows_after, (
        "the row the decision was made about is no longer current -- a resolution "
        "keyed on record.id would be pointing at it right now (TR-508)"
    )

    # --- and the decision still holds ---------------------------------------
    after = run(db_session, seeded_sources)

    pair = pair_for(after, PAIRED_LEFT)
    assert pair is not None, "the manual pair did not survive the correction (TR-317)"
    assert pair.origin is PairOrigin.MANUAL
    assert pair.right.reference == PAIRED_RIGHT

    stored = db_session.get(Resolution, decision.id)
    assert stored is not None
    assert stored.reason == REASON
    assert stored.author == AUTHOR
    assert stored.revoked_at is None

    # The pair is re-compared, not remembered. Its right-hand record is now the
    # version-2 row, and the run reads version-2 values throughout -- which the
    # brief's own worked example shows by flipping from break to agreed.
    resolved_row_after = db_session.scalar(
        select(Record)
        .join(FileBatch, Record.batch_id == FileBatch.id)
        .where(
            Record.source_id == seeded_sources.statement.id,
            Record.reference == PAIRED_RIGHT,
            FileBatch.version_no == 2,
        )
    )
    assert resolved_row_after is not None
    assert resolved_row_after.id != resolved_row_before.id
    assert pair.right.gross_amount == resolved_row_after.gross_amount
    assert compared(pair).verdict is Verdict.BREAK  # still twelve minutes apart

    corrected_after = pair_for(after, CORRECTED_REFERENCE)
    assert corrected_after is not None
    assert corrected_after.right.gross_amount != corrected_before.right.gross_amount
    assert compared(corrected_after).verdict is Verdict.AGREED


def test_a_resolution_naming_a_withdrawn_record_goes_inert_not_wrong(
    db_session: Session, seeded_sources
) -> None:
    """A correction can remove the row a decision was about.

    ``core.match`` treats a resolution naming a record this run does not contain
    as inert rather than as an error -- the file may simply not have arrived
    yet. The decision is not revoked either, because nobody decided to revoke
    it: it is still there, still live, and applies again the day the row comes
    back (TR-708).
    """
    load_both(db_session, seeded_sources)
    decision = manual_match(
        db_session,
        seeded_sources.ledger,
        "T-1021",
        seeded_sources.statement,
        "C-9002",  # dropped by the correction
        "their late booking of our T-1021",
        AUTHOR,
    )
    assert pair_for(run(db_session, seeded_sources), "T-1021") is not None

    load(db_session, seeded_sources.statement, CORRECTED_FILE)
    after = run(db_session, seeded_sources)

    assert pair_for(after, "T-1021") is None
    assert "T-1021" in references(after.unmatched_left)

    stored = db_session.get(Resolution, decision.id)
    assert stored is not None
    assert stored.revoked_at is None, "nothing revoked it, so nothing may pretend it did"


# ---------------------------------------------------------------------------
# AC10 -- accepting a record as having no counterpart
# ---------------------------------------------------------------------------


def test_accepted_unmatched_stays_out_of_the_worklist(db_session: Session, seeded_sources) -> None:
    """R7.2, AC10. Accepted once, gone from the worklist on every later run.

    Including after a correction, which is the version of this that a
    row-id-keyed implementation would fail.
    """
    load_both(db_session, seeded_sources)
    first = run(db_session, seeded_sources)
    assert LONELY_LEFT in references(first.unmatched_left)

    accept_unmatched(
        db_session,
        seeded_sources.ledger,
        LONELY_LEFT,
        "confirmed by phone: they never booked it",
        AUTHOR,
    )

    def assert_settled(stage: str) -> None:
        result = run(db_session, seeded_sources)
        assert LONELY_LEFT not in references(result.unmatched_left), stage
        assert LONELY_LEFT in references(result.accepted_unmatched), stage
        assert LONELY_LEFT not in references([s.left for s in result.suggestions]), stage

    assert_settled("re-run")
    load(db_session, seeded_sources.statement, CORRECTED_FILE)
    assert_settled("after the correction")


def test_an_acceptance_on_their_side_is_carried_forward_too(
    db_session: Session, seeded_sources
) -> None:
    """An acceptance names one identity and does not care which side it is on."""
    load_both(db_session, seeded_sources)
    accept_unmatched(
        db_session,
        seeded_sources.statement,
        LONELY_RIGHT,
        "their own fee booking, no counterpart expected",
        AUTHOR,
    )
    result = run(db_session, seeded_sources)
    assert LONELY_RIGHT not in references(result.unmatched_right)
    assert LONELY_RIGHT in references(result.accepted_unmatched)


# ---------------------------------------------------------------------------
# TR-310 -- a rejected suggestion is never offered again
# ---------------------------------------------------------------------------


def test_a_rejected_suggestion_is_never_offered_again(db_session: Session, seeded_sources) -> None:
    """R4.6, TR-310. Rejecting is a decision too, and it also has to stick."""
    load_both(db_session, seeded_sources)
    assert suggestion_for(run(db_session, seeded_sources), PAIRED_LEFT, PAIRED_RIGHT) is not None

    reject_suggestion(
        db_session,
        seeded_sources.ledger,
        PAIRED_LEFT,
        seeded_sources.statement,
        PAIRED_RIGHT,
        "different trades; the quantities agreeing is a coincidence",
        AUTHOR,
    )

    after = run(db_session, seeded_sources)
    assert suggestion_for(after, PAIRED_LEFT, PAIRED_RIGHT) is None

    # Rejecting a pairing settles neither record. Both are still unmatched and
    # still need a person -- a rejection is not a quiet way to close an item.
    assert PAIRED_LEFT in references(after.unmatched_left)
    assert PAIRED_RIGHT in references(after.unmatched_right)


def test_a_rejection_survives_a_correction(db_session: Session, seeded_sources) -> None:
    """The blocklist keys on identity like everything else here (TR-508)."""
    load_both(db_session, seeded_sources)
    reject_suggestion(
        db_session,
        seeded_sources.ledger,
        PAIRED_LEFT,
        seeded_sources.statement,
        PAIRED_RIGHT,
        "different trades",
        AUTHOR,
    )
    load(db_session, seeded_sources.statement, CORRECTED_FILE)
    assert suggestion_for(run(db_session, seeded_sources), PAIRED_LEFT, PAIRED_RIGHT) is None


# ---------------------------------------------------------------------------
# TR-708, R7.8 -- revocation is recorded, never a delete
# ---------------------------------------------------------------------------


def test_revocation_is_recorded_and_stops_applying(db_session: Session, seeded_sources) -> None:
    """R7.8, TR-708. The row survives its own revocation.

    A run that happened while the decision stood has to stay explicable
    afterwards, so the decision, its reason, its author, and the reason it was
    withdrawn are all still there.
    """
    load_both(db_session, seeded_sources)
    decision = manual_match(
        db_session,
        seeded_sources.ledger,
        PAIRED_LEFT,
        seeded_sources.statement,
        PAIRED_RIGHT,
        REASON,
        AUTHOR,
    )
    assert pair_for(run(db_session, seeded_sources), PAIRED_LEFT) is not None

    revoked = revoke(db_session, decision.id, "wrong reference; theirs was a different trade")

    assert revoked.id == decision.id
    assert revoked.revoked_at is not None
    assert revoked.revoked_at.tzinfo is not None
    assert revoked.revoked_reason == "wrong reference; theirs was a different trade"
    assert revoked.reason == REASON, "the original reason is not overwritten"
    assert revoked.author == AUTHOR

    still_there = db_session.scalar(select(Resolution).where(Resolution.id == decision.id))
    assert still_there is not None, "TR-708: nothing is hard-deleted"

    assert prior_resolutions(db_session, seeded_sources.ledger, seeded_sources.statement) == []

    # Undoing the decision puts the item back exactly where it was before
    # anyone touched it: offered again as a suggestion, awaiting a person.
    after = run(db_session, seeded_sources)
    assert pair_for(after, PAIRED_LEFT) is None
    assert suggestion_for(after, PAIRED_LEFT, PAIRED_RIGHT) is not None


def test_revoking_twice_is_refused_rather_than_silently_ignored(
    db_session: Session, seeded_sources
) -> None:
    load_both(db_session, seeded_sources)
    decision = accept_unmatched(
        db_session, seeded_sources.ledger, LONELY_LEFT, "no counterpart", AUTHOR
    )
    revoke(db_session, decision.id, "changed my mind")
    with pytest.raises(ResolutionError, match="already revoked"):
        revoke(db_session, decision.id, "changed it again")


def test_a_later_decision_supersedes_the_earlier_one_rather_than_deleting_it(
    db_session: Session, seeded_sources
) -> None:
    """Two live decisions about one record would make matching depend on query order.

    So the earlier one is retired -- recorded as a revocation naming what
    replaced it, never removed (TR-708).
    """
    load_both(db_session, seeded_sources)
    first = accept_unmatched(
        db_session, seeded_sources.ledger, PAIRED_LEFT, "they never booked it", AUTHOR
    )
    second = manual_match(
        db_session,
        seeded_sources.ledger,
        PAIRED_LEFT,
        seeded_sources.statement,
        PAIRED_RIGHT,
        REASON,
        AUTHOR,
    )

    assert first.revoked_at is not None
    assert "superseded" in (first.revoked_reason or "")
    assert second.revoked_at is None

    carried = prior_resolutions(db_session, seeded_sources.ledger, seeded_sources.statement)
    assert [r.kind for r in carried] == [ResolutionKind.MANUAL_MATCH]

    result = run(db_session, seeded_sources)
    assert pair_for(result, PAIRED_LEFT) is not None
    assert PAIRED_LEFT not in references(result.accepted_unmatched)


# ---------------------------------------------------------------------------
# TR-313, AC11 -- the one automatic reversal, end to end
# ---------------------------------------------------------------------------


def statement_with_the_missing_row() -> bytes:
    """The statement file, plus the row the counterparty forgot to send.

    ``data/statement_2025-07-01_07_v2.csv`` is a correction that *changes* three
    amounts and *drops* one row; no shipped sample file **adds** one, and R7.7
    is specifically about a correction that supplies a counterpart. So this
    builds that file out of the real one: the same bytes with the counterparty's
    booking of our T-1012 appended, in their own format.
    """
    body = STATEMENT_FILE.read_bytes()
    if not body.endswith(b"\n"):
        body += b"\n"
    return body + b"T-1012,2025-07-03 06:00:00,BTC-USD,B,0.2,63200,12640.00,SETTLED\n"


def test_an_acceptance_is_auto_revoked_when_a_correction_supplies_the_counterpart(
    db_session: Session, seeded_sources
) -> None:
    """TR-313, R7.7, D10, AC11. The one automatic reversal, and never a silent one.

    Yesterday's decision was honest and was made on incomplete information.
    Honouring it today would hide exactly what the correction fixed, so it is
    revoked -- and reported, so the analyst learns that a decision of theirs was
    reversed rather than discovering it by noticing an item they thought they
    had closed.
    """
    load_both(db_session, seeded_sources)
    assert LONELY_LEFT in references(run(db_session, seeded_sources).unmatched_left)

    accepted = accept_unmatched(
        db_session,
        seeded_sources.ledger,
        LONELY_LEFT,
        "confirmed by phone: they never booked it",
        AUTHOR,
    )
    settled = run(db_session, seeded_sources)
    assert LONELY_LEFT in references(settled.accepted_unmatched)
    assert settled.revocations == ()

    # --- the correction that supplies the counterpart ----------------------
    load(
        db_session,
        seeded_sources.statement,
        CORRECTED_FILE,
        content=statement_with_the_missing_row(),
    )

    result = run(db_session, seeded_sources)

    reported = [r for r in result.revocations if r.key.reference == LONELY_LEFT]
    assert len(reported) == 1, "the reversal has to be reported, not just performed"
    assert reported[0].key.source_code == seeded_sources.ledger.code
    assert "counterpart" in reported[0].reason

    paired = pair_for(result, LONELY_LEFT)
    assert paired is not None
    assert paired.origin is PairOrigin.REFERENCE
    assert LONELY_LEFT not in references(result.accepted_unmatched)

    # --- and the reversal is recorded against the decision it reversed ------
    governing = resolution_for(db_session, seeded_sources.ledger, LONELY_LEFT)
    assert governing is not None
    assert governing.id == accepted.id

    revoke(db_session, governing.id, reported[0].reason)

    stored = db_session.get(Resolution, accepted.id)
    assert stored is not None, "TR-708: an auto-revocation is a stamp, not a delete"
    assert stored.revoked_at is not None
    assert stored.revoked_reason == reported[0].reason
    assert stored.author == AUTHOR

    final = run(db_session, seeded_sources)
    assert pair_for(final, LONELY_LEFT) is not None
    assert final.revocations == (), "a revoked acceptance is not reported again every morning"
    assert resolution_for(db_session, seeded_sources.ledger, LONELY_LEFT) is None


# ---------------------------------------------------------------------------
# R7.3 -- a decision nobody signed is not a decision
# ---------------------------------------------------------------------------


def _constructors(sources) -> dict[str, Callable[[Session, str, str], Resolution]]:
    return {
        "manual_match": lambda session, reason, author: manual_match(
            session, sources.ledger, PAIRED_LEFT, sources.statement, PAIRED_RIGHT, reason, author
        ),
        "accept_unmatched": lambda session, reason, author: accept_unmatched(
            session, sources.ledger, LONELY_LEFT, reason, author
        ),
        "reject_suggestion": lambda session, reason, author: reject_suggestion(
            session, sources.ledger, PAIRED_LEFT, sources.statement, PAIRED_RIGHT, reason, author
        ),
    }


@pytest.mark.parametrize("kind", ["manual_match", "accept_unmatched", "reject_suggestion"])
@pytest.mark.parametrize(
    ("reason", "author", "missing"),
    [
        ("", AUTHOR, "reason"),
        ("   ", AUTHOR, "reason"),
        (REASON, "", "author"),
        (REASON, "\t", "author"),
    ],
)
def test_a_resolution_needs_a_reason_and_an_author(
    db_session: Session, seeded_sources, kind: str, reason: str, author: str, missing: str
) -> None:
    """R7.3, TR-604. The rule belongs to the decision, not to the form.

    TR-604 puts the check on the web layer, and it belongs there too -- but a
    service that accepts a blank reason leaves the form as the only thing
    between the audit trail and an empty string.
    """
    load_both(db_session, seeded_sources)
    with pytest.raises(ResolutionError, match=missing):
        _constructors(seeded_sources)[kind](db_session, reason, author)

    assert db_session.scalars(select(Resolution)).all() == []


def test_reason_and_author_are_stored_as_given_but_trimmed(
    db_session: Session, seeded_sources
) -> None:
    load_both(db_session, seeded_sources)
    stored = accept_unmatched(
        db_session, seeded_sources.ledger, f"  {LONELY_LEFT}  ", f"  {REASON}  ", f" {AUTHOR} "
    )
    assert stored.left_reference == LONELY_LEFT
    assert stored.reason == REASON
    assert stored.author == AUTHOR
    # The trimmed reference is the one the record actually has, so the decision
    # applies rather than sitting there looking correct and doing nothing.
    assert LONELY_LEFT in references(run(db_session, seeded_sources).accepted_unmatched)


def test_a_record_cannot_be_paired_with_itself(db_session: Session, seeded_sources) -> None:
    load_both(db_session, seeded_sources)
    with pytest.raises(ResolutionError, match="itself"):
        manual_match(
            db_session,
            seeded_sources.ledger,
            PAIRED_LEFT,
            seeded_sources.ledger,
            PAIRED_LEFT,
            REASON,
            AUTHOR,
        )


def test_revoking_something_that_does_not_exist_is_an_error_a_person_can_read(
    db_session: Session, seeded_sources
) -> None:
    with pytest.raises(ResolutionError, match="no resolution with id"):
        revoke(db_session, 9999, "typo")


# ---------------------------------------------------------------------------
# TR-508 -- structurally, not just behaviourally
# ---------------------------------------------------------------------------


def test_no_resolution_column_points_at_a_record_row() -> None:
    """TR-508, DD-5. The schema itself refuses the shortcut.

    A foreign key to ``record.id`` here would be simpler, would pass every test
    that never loads a correction, and would be silently wrong from the first
    one onwards.
    """
    targets = {
        key.target_fullname
        for column in Resolution.__table__.columns
        for key in column.foreign_keys
    }
    assert not [t for t in targets if t.startswith("record.")], (
        f"a resolution must name (source_id, reference), not a row: {sorted(targets)}"
    )
    assert {"left_source_id", "left_reference", "right_source_id", "right_reference"} <= set(
        Resolution.__table__.columns.keys()
    )


def test_the_resolution_service_never_reads_or_writes_a_record_id() -> None:
    """The same claim about the code, so the module cannot drift back.

    Parsed rather than grepped, so that the module is free to *explain* the rule
    in its own docstring without tripping the check that enforces it.
    """
    tree = ast.parse((ROOT / "app" / "services" / "resolve.py").read_text())
    offenders: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "id"
            and isinstance(node.value, ast.Name)
            and node.value.id == "Record"
        ):
            offenders.add("Record.id")
        for name in _identifiers(node):
            if "record_id" in name:
                offenders.add(name)
    assert not offenders, f"resolve.py must not key on a record row: {sorted(offenders)}"


def _identifiers(node: ast.AST) -> list[str]:
    """Every name this node introduces or reads. Strings and docstrings are not names."""
    for attribute in ("attr", "id", "arg"):
        value = getattr(node, attribute, None)
        if isinstance(value, str):
            return [value]
    return []


# ---------------------------------------------------------------------------
# AC9 and AC10, at the level they are written about: the worklist
# ---------------------------------------------------------------------------


def reconcile(session: Session, sources):
    return run_reconciliation(session, sources.ledger, sources.statement, *PERIOD)


def worklist_references(session: Session, run_id: int) -> set[str]:
    items = worklist(session, run_id)
    return {session.get(Record, item.record_id).reference for item in items}


def state_of(session: Session, run_id: int, source: Source, reference: str) -> str:
    item = session.scalar(
        select(RunItem)
        .join(Record, RunItem.record_id == Record.id)
        .where(
            RunItem.run_id == run_id,
            Record.source_id == source.id,
            Record.reference == reference,
        )
    )
    assert item is not None, f"{reference} was read by the run, so it must have a state (TR-509)"
    return item.state


@needs_the_reconciler
def test_a_manual_pair_survives_a_correction_through_the_reconciler(
    db_session: Session, seeded_sources
) -> None:
    """AC9. Pair two unmatched records, re-run, and they are still paired.

    The same claim as :func:`test_survives_correction`, made against the rows a
    run actually writes -- so that a resolution surviving in ``core.match`` but
    not surviving the trip through the database would still be caught.
    """
    load_both(db_session, seeded_sources)
    reconcile(db_session, seeded_sources)

    decision = manual_match(
        db_session,
        seeded_sources.ledger,
        PAIRED_LEFT,
        seeded_sources.statement,
        PAIRED_RIGHT,
        REASON,
        AUTHOR,
    )
    reconcile(db_session, seeded_sources)
    load(db_session, seeded_sources.statement, CORRECTED_FILE)
    final = reconcile(db_session, seeded_sources)

    pair = db_session.scalar(
        select(Pair)
        .join(Record, Pair.left_record_id == Record.id)
        .where(Pair.run_id == final.id, Record.reference == PAIRED_LEFT)
    )
    assert pair is not None, "the manual pair did not survive the correction"
    assert pair.origin == str(PairOrigin.MANUAL)

    right = db_session.get(Record, pair.right_record_id)
    assert right is not None
    assert right.reference == PAIRED_RIGHT
    batch = db_session.get(FileBatch, right.batch_id)
    assert batch is not None
    assert batch.version_no == 2, "the pair names the corrected rows, not the superseded ones"

    # "with the reason and author still shown" is the other half of AC9, and it
    # is reachable from the pair by identity rather than by row id.
    governing = resolution_for(db_session, seeded_sources.ledger, PAIRED_LEFT)
    assert governing is not None
    assert governing.id == decision.id
    assert (governing.reason, governing.author) == (REASON, AUTHOR)


@needs_the_reconciler
def test_an_accepted_record_is_absent_from_the_worklist(
    db_session: Session, seeded_sources
) -> None:
    """AC10. Accepting a record as unmatched takes it off the morning's list."""
    load_both(db_session, seeded_sources)
    before = reconcile(db_session, seeded_sources)
    assert LONELY_LEFT in worklist_references(db_session, before.id)
    assert state_of(db_session, before.id, seeded_sources.ledger, LONELY_LEFT) == str(
        RecordState.UNMATCHED
    )

    accept_unmatched(
        db_session,
        seeded_sources.ledger,
        LONELY_LEFT,
        "confirmed by phone: they never booked it",
        AUTHOR,
    )

    after = reconcile(db_session, seeded_sources)
    assert LONELY_LEFT not in worklist_references(db_session, after.id)
    assert state_of(db_session, after.id, seeded_sources.ledger, LONELY_LEFT) == str(
        RecordState.ACCEPTED_UNMATCHED
    )

    # Still counted, still explicable: leaving the worklist is not disappearing.
    assert after.counts.get(str(RecordState.ACCEPTED_UNMATCHED)) == 1


@needs_the_reconciler
def test_a_revoked_decision_stops_applying_to_the_next_run(
    db_session: Session, seeded_sources
) -> None:
    """R7.8. Undo puts the item back on the list, and the run after it agrees."""
    load_both(db_session, seeded_sources)
    decision = accept_unmatched(
        db_session, seeded_sources.ledger, LONELY_LEFT, "they never booked it", AUTHOR
    )
    settled = reconcile(db_session, seeded_sources)
    assert LONELY_LEFT not in worklist_references(db_session, settled.id)

    revoke(db_session, decision.id, "they did book it after all")

    reopened = reconcile(db_session, seeded_sources)
    assert LONELY_LEFT in worklist_references(db_session, reopened.id)
    assert state_of(db_session, reopened.id, seeded_sources.ledger, LONELY_LEFT) == str(
        RecordState.UNMATCHED
    )


def test_a_pairing_recorded_the_other_way_round_is_still_applied(
    db_session: Session, seeded_sources
) -> None:
    """R7.4. Orientation belongs to the run, not to the decision.

    Which source a run calls "left" is a property of the run. A decision is a
    statement about two identities, so one recorded statement-first has to apply
    to a ledger-first run rather than sitting there looking correct and doing
    nothing.
    """
    load_both(db_session, seeded_sources)
    manual_match(
        db_session,
        seeded_sources.statement,
        PAIRED_RIGHT,
        seeded_sources.ledger,
        PAIRED_LEFT,
        REASON,
        AUTHOR,
    )

    carried = prior_resolutions(db_session, seeded_sources.ledger, seeded_sources.statement)
    assert [(r.left.source_code, r.right.source_code) for r in carried] == [
        (seeded_sources.ledger.code, seeded_sources.statement.code)
    ]

    pair = pair_for(run(db_session, seeded_sources), PAIRED_LEFT)
    assert pair is not None
    assert pair.origin is PairOrigin.MANUAL
    assert pair.right.reference == PAIRED_RIGHT


# ---------------------------------------------------------------------------
# A revocation must be written down, not merely announced
# ---------------------------------------------------------------------------

SECOND_CORRECTION = DATA / "statement_2025-07-01_07_v3.csv"
LATE_BOOKED = "T-1012"


def test_an_auto_revocation_is_recorded_and_not_re_announced_every_run(
    db_session: Session, seeded_sources
) -> None:
    """The reversal reaches the resolution table, and stops there.

    ``core.match`` decides an acceptance no longer holds, but deciding is not
    recording. When ``run_reconciliation`` computed the revocation and threw it
    away, the resolution row stayed live, the next run recomputed the same
    revocation from the same inputs, and the analyst was told again every
    morning for as long as the system ran. The third run below is the whole
    point of this test: the reversal must happen once.
    """
    from app.services.reconcile import run_reconciliation

    load_both(db_session, seeded_sources)
    run_reconciliation(db_session, seeded_sources.ledger, seeded_sources.statement, *PERIOD)

    # T-1012 is on our books and on no statement, so accepting it is reasonable.
    accepted = accept_unmatched(
        db_session,
        seeded_sources.ledger,
        LATE_BOOKED,
        reason="Counterparty confirmed by phone they have no record of it.",
        author="aoife",
    )
    assert accepted.revoked_at is None

    # The counterparty books it late, in a correction that ADDS a row.
    load(db_session, seeded_sources.statement, SECOND_CORRECTION)
    run_reconciliation(db_session, seeded_sources.ledger, seeded_sources.statement, *PERIOD)

    db_session.refresh(accepted)
    assert accepted.revoked_at is not None, (
        "the acceptance was withdrawn by the run but never written down"
    )
    assert "counterpart" in (accepted.revoked_reason or "")

    # Recorded, never deleted (TR-708).
    assert db_session.get(Resolution, accepted.id) is not None

    # And the third run is quiet: the decision is gone, so there is nothing
    # left to revoke and nothing to re-announce.
    again = run(db_session, seeded_sources)
    assert again.revocations == ()
    assert LATE_BOOKED in {p.left.reference for p in again.pairs}


def test_a_revoked_acceptance_stops_being_applied(db_session: Session, seeded_sources) -> None:
    """Once revoked, the decision no longer reaches matching at all."""
    load_both(db_session, seeded_sources)
    accepted = accept_unmatched(
        db_session,
        seeded_sources.ledger,
        LATE_BOOKED,
        reason="No counterpart expected.",
        author="aoife",
    )
    assert LATE_BOOKED in {r.reference for r in run(db_session, seeded_sources).accepted_unmatched}

    revoke(db_session, accepted.id, "Counterparty booked it after all.")
    after = run(db_session, seeded_sources)
    assert LATE_BOOKED not in {r.reference for r in after.accepted_unmatched}
