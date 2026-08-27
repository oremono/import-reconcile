"""Running a reconciliation: orchestration, and nothing else.

Every decision this module makes was already made in ``core``. Its job is to
fetch the right rows, hand them to :func:`core.match.match` and
:func:`core.compare.compare`, and write the answer down. No threshold, no
format detail and no matching rule appears here - if one ever does, it belongs
in ``core`` where it can be tested without a database.

Two properties are load-bearing and are asserted rather than hoped for:

**Every record read lands in exactly one state** (TR-509, AC2). ``core.match``
returns a total, disjoint partition of its inputs; this module turns that
partition into one ``run_item`` per record and refuses to finish if a record
went missing or was claimed twice. The summary then adds up by construction
rather than by coincidence.

**A run is append-only** (TR-502). Re-running writes a new ``Run``; no earlier
row is touched. That is what makes "what did the eleventh look like?" a query.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, cast

from sqlalchemy import Table, func, insert, select
from sqlalchemy.orm import Session

from app.db.models import (
    FieldDiffRow,
    FileBatch,
    Pair,
    Record,
    RejectedRow,
    Run,
    RunItem,
    Source,
    ToleranceProfile,
)
from app.observability import log_run
from app.services.ingest import current_batch, current_records, withdrawn_references
from app.services.resolve import prior_resolutions, resolution_for, revoke
from core.compare import compare
from core.format import source_format_from_config
from core.match import match
from core.model import (
    COMPARED_FIELDS,
    WORKLIST_STATES,
    Comparison,
    MatchedPair,
    MatchResult,
    NormalizedRecord,
    PairOrigin,
    PriorResolution,
    RecordKey,
    RecordState,
    RecordStatus,
    Side,
    Tolerances,
    Verdict,
)
from core.tolerance import tolerances_from_config

LEFT = "left"
RIGHT = "right"

#: Verdict on a pair maps one-to-one onto the state both its records end in.
_VERDICT_STATE: dict[Verdict, RecordState] = {
    Verdict.AGREED: RecordState.AGREED,
    Verdict.AGREED_WITH_DRIFT: RecordState.AGREED_WITH_DRIFT,
    Verdict.BREAK: RecordState.BREAK,
}


#: The two tables written a row at a time per record. Both are inserted through
#: a Core statement rather than the ORM one: neither needs an identity back, and
#: skipping the unit of work is roughly a third of the run budget at ten
#: thousand records a side (TR-701).
_FIELD_DIFF_TABLE: Table = cast(Table, FieldDiffRow.__table__)
_RUN_ITEM_TABLE: Table = cast(Table, RunItem.__table__)
_PAIR_TABLE: Table = cast(Table, Pair.__table__)


def _column_scale(column: Any) -> int:
    """The declared scale of a money column, read from the column itself.

    Written down here as well it would be a second copy of a number that only
    ``app/db/models.py`` gets to choose.
    """
    return int(column.type.scale)


_STORED_SCALE: int = _column_scale(_PAIR_TABLE.c.max_rel_diff)
_STORED_UNIT: Decimal = Decimal(1).scaleb(-_STORED_SCALE)

_NANOSECONDS_PER_SECOND = Decimal(1000000000)
_MILLISECOND = Decimal("0.001")


def _stored(value: Decimal | None) -> Decimal | None:
    """Round a derived magnitude to the scale its column is declared at.

    Only *relative* differences reach this. A relative difference is a quotient
    and so is usually non-terminating, while ``ExactDecimal`` refuses to round
    silently - correctly, because a rounded amount is a wrong amount. Here the
    rounding is explicit and it costs nothing: the exact figures are already
    stored twice, as ``left_value`` and ``right_value`` on the same row, and
    this number exists to order a list. An absolute difference is a subtraction
    of two values already held at this scale, so passing it through changes
    nothing.
    """
    if value is None:
        return None
    return value.quantize(_STORED_UNIT, rounding=ROUND_HALF_EVEN)


class ReconcileError(Exception):
    """A run that cannot be started, for a reason a person can act on."""


# ---------------------------------------------------------------------------
# Marshalling: database rows in, core dataclasses out
# ---------------------------------------------------------------------------


def _normalized(record: Record, source_code: str) -> NormalizedRecord:
    """A stored row, back in the vocabulary ``core`` speaks.

    Nothing is re-parsed: the row was normalised once at ingest and stored at
    the precision it arrived in. This only re-wraps it.
    """
    return NormalizedRecord(
        source_code=source_code,
        reference=record.reference,
        occurred_at=record.occurred_at,
        instrument=record.instrument,
        side=Side(record.side),
        quantity=record.quantity,
        unit_price=record.unit_price,
        gross_amount=record.gross_amount,
        status=RecordStatus(record.status),
        row_no=record.row_no,
        raw={},
    )


def _profile_config(profile: ToleranceProfile) -> dict[str, str]:
    """The stored row as the text mapping ``tolerances_from_config`` validates.

    Routed through the same parser the seed and the tests use, so a profile that
    would fail at startup fails here too rather than being trusted because it
    came out of a column (TR-405).
    """
    return {
        "amount_bps": format(profile.amount_bps, "f"),
        "amount_abs_floor": format(profile.amount_abs_floor, "f"),
        "price_bps": format(profile.price_bps, "f"),
        "qty_bps": format(profile.qty_bps, "f"),
        "time_tolerance_seconds": str(profile.time_tolerance_seconds),
        "suggest_window_seconds": str(profile.suggest_window_seconds),
    }


def tolerances_for(session: Session, left: Source, right: Source) -> Tolerances:
    """The profile for this ordered source pair. Absent is an error, not a default.

    Falling back to a built-in default would put a threshold in code, which is
    the one thing CLAUDE.md invariant 7 forbids.
    """
    profile = session.scalar(
        select(ToleranceProfile).where(
            ToleranceProfile.left_source_id == left.id,
            ToleranceProfile.right_source_id == right.id,
        )
    )
    if profile is None:
        raise ReconcileError(
            f"no tolerance profile for {left.code} against {right.code}; "
            "a source pair is reconciled only once its thresholds are configured"
        )
    return tolerances_from_config(_profile_config(profile))


def _keyed(records: Sequence[Record], source_code: str) -> dict[RecordKey, Record]:
    """Index the side by business identity, refusing an ambiguous file.

    ``core.match`` keys on ``RecordKey`` throughout, so two rows sharing a
    reference would collapse into one and the state counts would quietly fail
    to add up. Better to refuse the run and name the references (D19: a
    reference is never reused).
    """
    keyed: dict[RecordKey, Record] = {}
    duplicates: list[str] = []
    for record in records:
        key = RecordKey(source_code, record.reference)
        if key in keyed:
            duplicates.append(record.reference)
            continue
        keyed[key] = record
    if duplicates:
        shown = ", ".join(sorted(set(duplicates))[:5])
        raise ReconcileError(
            f"{source_code} reports {len(set(duplicates))} reference(s) more than once "
            f"({shown}); a reference identifies one transaction, so the file is ambiguous"
        )
    return keyed


# ---------------------------------------------------------------------------
# Turning a MatchResult into one state per record
# ---------------------------------------------------------------------------


class _States:
    """One state per record key, and a loud complaint if that is ever violated."""

    def __init__(self) -> None:
        self.by_key: dict[RecordKey, RecordState] = {}

    def claim(self, key: RecordKey, state: RecordState) -> None:
        existing = self.by_key.get(key)
        if existing is not None and existing is not state:
            raise ReconcileError(
                f"{key.source_code}/{key.reference} was assigned both {existing} "
                f"and {state}; the match partition is meant to be disjoint (TR-509)"
            )
        self.by_key[key] = state

    def claim_all(self, records: Iterable[NormalizedRecord], state: RecordState) -> None:
        for record in records:
            self.claim(record.key, state)


def _assign_states(result: MatchResult, verdicts: Mapping[RecordKey, Verdict]) -> _States:
    """Fold every bucket of a ``MatchResult`` into the state its records end in."""
    states = _States()

    states.claim_all(result.excluded_left, RecordState.EXCLUDED)
    states.claim_all(result.excluded_right, RecordState.EXCLUDED)
    states.claim_all(result.status_disagreements, RecordState.STATUS_DISAGREEMENT)
    states.claim_all(result.accepted_unmatched, RecordState.ACCEPTED_UNMATCHED)
    states.claim_all(result.unmatched_left, RecordState.UNMATCHED)
    states.claim_all(result.unmatched_right, RecordState.UNMATCHED)

    # A record may be proposed against several counterparts; the state is the
    # same either way, so the many-to-many collapses to one row per record.
    for suggestion in result.suggestions:
        states.claim(suggestion.left.key, RecordState.SUGGESTED)
        states.claim(suggestion.right.key, RecordState.SUGGESTED)

    for pair in result.pairs:
        state = _VERDICT_STATE[verdicts[pair.left.key]]
        states.claim(pair.left.key, state)
        states.claim(pair.right.key, state)

    return states


# ---------------------------------------------------------------------------
# Withdrawal and rejection: the rows the files no longer, or never, contained
# ---------------------------------------------------------------------------


def _withdrawn_records(session: Session, source: Source, start: date, end: date) -> list[Record]:
    """The last stored row for each reference an earlier version had and this one drops.

    A withdrawn row is not in the current batch, so it cannot be read from
    there; the record it points at is the newest superseded one, which is
    exactly what an analyst asking "what did it say before it vanished?"
    wants to see (TR-107).
    """
    references = withdrawn_references(session, source, start, end)
    if not references:
        return []
    rows = session.scalars(
        select(Record)
        .join(FileBatch, Record.batch_id == FileBatch.id)
        .where(
            Record.source_id == source.id,
            FileBatch.period_start == start,
            FileBatch.period_end == end,
            FileBatch.superseded_by_id.is_not(None),
            Record.reference.in_(references),
        )
        .order_by(FileBatch.version_no)
    )
    latest: dict[str, Record] = {}
    for row in rows:
        latest[row.reference] = row  # ordered ascending, so the last write wins
    return list(latest.values())


def _rejected_count(session: Session, source: Source, start: date, end: date) -> int:
    """Rows of the current delivery that could not be loaded at all (TR-106).

    Counted from ``rejected_row`` rather than from ``file_batch.rejected_count``
    so the number is the rows a person can actually open and look at.
    """
    batch = current_batch(session, source, start, end)
    if batch is None:
        return 0
    total = session.scalar(
        select(func.count()).select_from(RejectedRow).where(RejectedRow.batch_id == batch.id)
    )
    return int(total or 0)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def run_reconciliation(
    session: Session,
    left_source: Source,
    right_source: Source,
    period_start: date,
    period_end: date,
) -> Run:
    """Reconcile one period between two sources, and write the result down.

    Append-only (TR-502): this always creates a new :class:`Run`. Nothing about
    an earlier run is read, edited or invalidated, so two runs over the same
    period are two answers that can be compared rather than one answer that
    overwrote another.
    """
    started_ns = time.monotonic_ns()

    # Validated before a single row is read. A source whose format config no
    # longer parses cannot be reconciled, and finding that out ten thousand
    # rows in is worse than not starting (TR-203, TR-705).
    source_format_from_config(left_source.code, left_source.format_config)
    source_format_from_config(right_source.code, right_source.format_config)
    tolerances = tolerances_for(session, left_source, right_source)

    left_rows = _keyed(
        current_records(session, left_source, period_start, period_end), left_source.code
    )
    right_rows = _keyed(
        current_records(session, right_source, period_start, period_end), right_source.code
    )

    prior = _load_prior_resolutions(session, left_source, right_source)

    result = match(
        [_normalized(r, left_source.code) for r in left_rows.values()],
        [_normalized(r, right_source.code) for r in right_rows.values()],
        tolerances,
        prior,
    )

    run = Run(
        left_source_id=left_source.id,
        right_source_id=right_source.id,
        period_start=period_start,
        period_end=period_end,
        started_at=datetime.now(UTC),
        counts={},
    )
    session.add(run)
    session.flush()

    comparisons = {p.left.key: compare(p.left, p.right, tolerances) for p in result.pairs}
    verdicts = {key: c.verdict for key, c in comparisons.items()}

    sources = {left_source.code: left_source, right_source.code: right_source}
    pair_ids = _persist_pairs(session, run, result, comparisons, left_rows, right_rows, sources)
    _persist_revocations(session, result, sources)

    states = _assign_states(result, verdicts)
    _guard_total(states, left_rows, right_rows)

    withdrawn = [
        (LEFT, r) for r in _withdrawn_records(session, left_source, period_start, period_end)
    ] + [(RIGHT, r) for r in _withdrawn_records(session, right_source, period_start, period_end)]

    _persist_items(session, run, states, pair_ids, left_rows, right_rows, withdrawn)

    rejected = _rejected_count(session, left_source, period_start, period_end) + _rejected_count(
        session, right_source, period_start, period_end
    )

    counts = _counts(states, withdrawn_count=len(withdrawn), rejected_count=rejected)
    run.counts = counts
    run.finished_at = datetime.now(UTC)
    session.flush()

    log_run(
        run_id=run.id,
        left_source=left_source.code,
        right_source=right_source.code,
        period_start=period_start,
        period_end=period_end,
        counts=counts,
        records_read=len(left_rows) + len(right_rows),
        versions=_versions(session, (left_source, right_source), period_start, period_end),
        duration_seconds=(
            Decimal(time.monotonic_ns() - started_ns) / _NANOSECONDS_PER_SECOND
        ).quantize(_MILLISECOND, rounding=ROUND_HALF_EVEN),
    )
    return run


def _load_prior_resolutions(
    session: Session, left_source: Source, right_source: Source
) -> Sequence[PriorResolution]:
    """Decisions a person already made, applied before anything automatic.

    Read on every run rather than copied forward, because a resolution is a
    statement about identity and not about the run it was made in (R7.4). This
    is the whole of "a decision must never need to be made twice": there is no
    other path by which yesterday's work reaches today's matching.
    """
    return prior_resolutions(session, left_source, right_source)


def _persist_revocations(
    session: Session, result: MatchResult, sources: Mapping[str, Source]
) -> int:
    """Write down the acceptances this run withdrew.

    ``core.match`` decides that an acceptance no longer holds, but deciding is
    not recording. Without this the ``resolution`` row stays live, the next run
    recomputes the same revocation from the same inputs, and the analyst is told
    again every morning for as long as the system runs. R7.7 says the reversal
    is reported; TR-708 says it is recorded rather than deleted.
    """
    written = 0
    for revocation in result.revocations:
        source = sources.get(revocation.key.source_code)
        if source is None:
            continue
        existing = resolution_for(session, source, revocation.key.reference)
        if existing is None or existing.revoked_at is not None:
            continue
        revoke(session, existing.id, revocation.reason)
        written += 1
    return written


def _versions(
    session: Session, sources: Sequence[Source], start: date, end: date
) -> dict[str, int]:
    """The file version each side was read at. Part of what makes a run reproducible."""
    versions: dict[str, int] = {}
    for source in sources:
        batch = current_batch(session, source, start, end)
        if batch is not None:
            versions[source.code] = batch.version_no
    return versions


def _guard_total(
    states: _States,
    left_rows: Mapping[RecordKey, Record],
    right_rows: Mapping[RecordKey, Record],
) -> None:
    """TR-509 made structural: refuse to finish a run that lost a record.

    ``core.match`` promises a total partition. This is the assertion that turns
    the promise into a failure the moment it stops holding, rather than into a
    summary that is quietly short by three.
    """
    expected = set(left_rows) | set(right_rows)
    missing = expected - set(states.by_key)
    extra = set(states.by_key) - expected
    if missing or extra:
        raise ReconcileError(
            f"the match partition is not total: {len(missing)} record(s) ended in no state "
            f"and {len(extra)} state(s) name no record (TR-509)"
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _resolution_behind(
    session: Session,
    pair: MatchedPair,
    left_rows: Mapping[RecordKey, Record],
    sources: Mapping[str, Source],
) -> int | None:
    """The resolution that created this pair, if a person created it.

    Looked up by business identity like everything else about a resolution, so
    a correction cannot detach it (TR-508).
    """
    if pair.origin is not PairOrigin.MANUAL:
        return None
    source = sources.get(pair.left.source_code)
    if source is None:
        return None
    found = resolution_for(session, source, pair.left.reference)
    return found.id if found is not None else None


def _persist_pairs(
    session: Session,
    run: Run,
    result: MatchResult,
    comparisons: Mapping[RecordKey, Comparison],
    left_rows: Mapping[RecordKey, Record],
    right_rows: Mapping[RecordKey, Record],
    sources: Mapping[str, Source],
) -> dict[RecordKey, int]:
    """One ``pair`` row per matched pair, and one ``field_diff`` row per compared field.

    Every compared field is stored, including the ones that agree, so the
    detail page renders the whole record from one query (TR-401).
    """
    rows = [
        Pair(
            run_id=run.id,
            left_record_id=left_rows[pair.left.key].id,
            right_record_id=right_rows[pair.right.key].id,
            origin=str(pair.origin),
            verdict=str(comparisons[pair.left.key].verdict),
            max_rel_diff=_stored(comparisons[pair.left.key].max_rel_diff) or Decimal(0),
            # A manual pair carries the decision that created it, so the detail
            # page can answer "who decided this, and why?" without guessing.
            resolution_id=_resolution_behind(session, pair, left_rows, sources),
        )
        for pair in result.pairs
    ]
    if not rows:
        return {}
    session.add_all(rows)
    session.flush()

    pair_ids = {pair.left.key: row.id for pair, row in zip(result.pairs, rows, strict=True)}
    pair_ids.update({pair.right.key: row.id for pair, row in zip(result.pairs, rows, strict=True)})

    session.execute(
        insert(_FIELD_DIFF_TABLE),
        [
            {
                "pair_id": row.id,
                "field": diff.field,
                "left_value": diff.left_value,
                "right_value": diff.right_value,
                "differs": diff.differs,
                "within_tolerance": diff.within_tolerance,
                "abs_diff": _stored(diff.abs_diff),
                "rel_diff": _stored(diff.rel_diff),
            }
            for pair, row in zip(result.pairs, rows, strict=True)
            for diff in comparisons[pair.left.key].diffs
        ],
    )
    return pair_ids


def _persist_items(
    session: Session,
    run: Run,
    states: _States,
    pair_ids: Mapping[RecordKey, int],
    left_rows: Mapping[RecordKey, Record],
    right_rows: Mapping[RecordKey, Record],
    withdrawn: Sequence[tuple[str, Record]],
) -> None:
    """One ``run_item`` per record, carrying exactly one state (TR-509)."""
    payload: list[dict[str, Any]] = []
    for side, rows in ((LEFT, left_rows), (RIGHT, right_rows)):
        for key, record in rows.items():
            payload.append(
                {
                    "run_id": run.id,
                    "record_id": record.id,
                    "side": side,
                    "state": str(states.by_key[key]),
                    "pair_id": pair_ids.get(key),
                }
            )
    # A withdrawn reference is absent from the current delivery, so its row is
    # the newest superseded one. It is still a finding, and it is still one
    # state (TR-107, SPEC 5.6).
    payload += [
        {
            "run_id": run.id,
            "record_id": record.id,
            "side": side,
            "state": str(RecordState.WITHDRAWN),
            "pair_id": None,
        }
        for side, record in withdrawn
    ]
    if payload:
        session.execute(insert(_RUN_ITEM_TABLE), payload)


def _counts(states: _States, *, withdrawn_count: int, rejected_count: int) -> dict[str, int]:
    """The run summary: every state named, including the ones that are zero.

    A stable shape means two runs can be diffed key by key without either
    having to be re-walked (TR-512), and a state that dropped to zero reads as
    progress rather than as a missing key.
    """
    counts = {str(state): 0 for state in RecordState}
    for state in states.by_key.values():
        counts[str(state)] += 1
    counts[str(RecordState.WITHDRAWN)] += withdrawn_count
    # A rejected row never became a record, so it has no run_item to be counted
    # from. It is still a row the file contained and still needs an outcome, so
    # it is added here - which is what makes the summary account for every line
    # of both files rather than only the ones that parsed (AC2).
    counts[str(RecordState.REJECTED_ROW)] += rejected_count
    return counts


# ---------------------------------------------------------------------------
# Reading a run back
# ---------------------------------------------------------------------------


def run_summary(session: Session, run_id: int) -> dict[str, int]:
    """State counts for one run, from the summary stored on the run itself.

    Read from ``run.counts`` rather than recomputed, which is the point of
    TR-512: comparing this morning's run with yesterday's is two row reads, not
    two full walks of ``run_item``. It is also the only place rejected rows can
    be counted from, since a row that never parsed has no record to point at.
    """
    run = session.get(Run, run_id)
    if run is None:
        raise ReconcileError(f"no run {run_id}")
    stored = dict(run.counts or {})
    counts = {str(state): 0 for state in RecordState}
    for name, value in stored.items():
        counts[name] = int(value)
    return counts


def worklist(session: Session, run_id: int, state: str | None = None) -> list[RunItem]:
    """The records that need a person, worst first (TR-606).

    Ordered by size of difference descending, then by reference, so the largest
    break is the first thing on the screen. Items with no pair carry no
    magnitude; they sort after every pair that does, which is why the ordering
    is written as "has a difference at all" before "how big" - the two backends
    disagree about where NULLs belong and no code here may ask which one it got
    (TR-704).
    """
    wanted = {str(s) for s in WORKLIST_STATES}
    if state is not None:
        wanted &= {state}
    if not wanted:
        return []

    statement = (
        select(RunItem)
        .join(Record, RunItem.record_id == Record.id)
        .join(Pair, RunItem.pair_id == Pair.id, isouter=True)
        .where(RunItem.run_id == run_id, RunItem.state.in_(sorted(wanted)))
        .order_by(
            Pair.max_rel_diff.is_(None).asc(),
            Pair.max_rel_diff.desc(),
            Record.reference.asc(),
        )
    )
    return list(session.scalars(statement))


def pair_detail(session: Session, pair_id: int) -> tuple[Pair, Record, Record, list[FieldDiffRow]]:
    """One pair, both its records, and every compared field in render order."""
    pair = session.get(Pair, pair_id)
    if pair is None:
        raise ReconcileError(f"no pair {pair_id}")
    left = session.get(Record, pair.left_record_id)
    right = session.get(Record, pair.right_record_id)
    if left is None or right is None:
        raise ReconcileError(f"pair {pair_id} names a record that does not exist")

    diffs = list(session.scalars(select(FieldDiffRow).where(FieldDiffRow.pair_id == pair_id)))
    order = {field: index for index, field in enumerate(COMPARED_FIELDS)}
    diffs.sort(key=lambda d: (order.get(d.field, len(order)), d.field))
    return pair, left, right, diffs


def candidates_for(session: Session, run_id: int, record_id: int) -> list[tuple[Record, str]]:
    """Ranked counterparts proposed for one record, each with why it qualifies (TR-608).

    Recomputed from the same pure function the run used rather than stored,
    because a suggestion is never applied and so has nothing to persist
    (invariant 9). Same inputs, same order, every time.
    """
    run = session.get(Run, run_id)
    record = session.get(Record, record_id)
    if run is None or record is None:
        return []

    left_source = session.get(Source, run.left_source_id)
    right_source = session.get(Source, run.right_source_id)
    if left_source is None or right_source is None:
        return []

    tolerances = tolerances_for(session, left_source, right_source)
    left_rows = _keyed(
        current_records(session, left_source, run.period_start, run.period_end), left_source.code
    )
    right_rows = _keyed(
        current_records(session, right_source, run.period_start, run.period_end),
        right_source.code,
    )
    result = match(
        [_normalized(r, left_source.code) for r in left_rows.values()],
        [_normalized(r, right_source.code) for r in right_rows.values()],
        tolerances,
        _load_prior_resolutions(session, left_source, right_source),
    )

    subject = RecordKey(
        left_source.code if record.source_id == left_source.id else right_source.code,
        record.reference,
    )
    found: list[tuple[int, str, Record, str]] = []
    for suggestion in result.suggestions:
        if suggestion.left.key == subject:
            other = right_rows.get(suggestion.right.key)
        elif suggestion.right.key == subject:
            other = left_rows.get(suggestion.left.key)
        else:
            continue
        if other is not None:
            found.append((suggestion.rank, other.reference, other, suggestion.reason))
    found.sort(key=lambda item: (item[0], item[1]))
    return [(other, reason) for _, _, other, reason in found]
