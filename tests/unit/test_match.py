"""Matching, tier by tier.

No database, no application, no browser - the brief names this suite
explicitly. Records are built in Python and assertions are made on the returned
dataclasses.

The test that matters most is ``test_partition_is_total_and_disjoint``: every
record handed in comes back in exactly one bucket. If that holds, the run
summary cannot fail to add up, which is acceptance criterion 2.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import core.match as match_module
from core.match import match
from core.model import (
    NormalizedRecord,
    PairOrigin,
    PriorResolution,
    RecordKey,
    RecordStatus,
    ResolutionKind,
    Side,
    Tolerances,
)

BASE = datetime(2025, 7, 1, 9, 0, tzinfo=UTC)

TOL = Tolerances(
    amount_bps=Decimal("0.0005"),
    amount_abs_floor=Decimal("0.01"),
    price_bps=Decimal("0.0005"),
    qty_bps=Decimal("0.0001"),
    time_tolerance_seconds=300,
    suggest_window_seconds=7200,
)


def rec(
    reference: str,
    *,
    source: str = "ledger",
    minutes: int = 0,
    instrument: str = "BTC-USD",
    side: Side = Side.BUY,
    quantity: str = "1.00",
    unit_price: str = "62000.00",
    gross_amount: str = "62000.00",
    status: RecordStatus = RecordStatus.SETTLED,
    row_no: int = 2,
) -> NormalizedRecord:
    return NormalizedRecord(
        source_code=source,
        reference=reference,
        occurred_at=BASE + timedelta(minutes=minutes),
        instrument=instrument,
        side=side,
        quantity=Decimal(quantity),
        unit_price=Decimal(unit_price),
        gross_amount=Decimal(gross_amount),
        status=status,
        row_no=row_no,
        raw={},
    )


def right(reference: str, **kwargs: object) -> NormalizedRecord:
    kwargs.setdefault("source", "statement")
    return rec(reference, **kwargs)  # type: ignore[arg-type]


def every_record(result: object) -> list[tuple[str, str]]:
    """Flatten a MatchResult into (source, reference) pairs, one per appearance."""
    r = result
    out: list[tuple[str, str]] = []
    for pair in r.pairs:  # type: ignore[attr-defined]
        out += [(pair.left.source_code, pair.left.reference)]
        out += [(pair.right.source_code, pair.right.reference)]
    for group in (
        r.unmatched_left,  # type: ignore[attr-defined]
        r.unmatched_right,  # type: ignore[attr-defined]
        r.excluded_left,  # type: ignore[attr-defined]
        r.excluded_right,  # type: ignore[attr-defined]
        r.accepted_unmatched,  # type: ignore[attr-defined]
        r.status_disagreements,  # type: ignore[attr-defined]
    ):
        out += [(x.source_code, x.reference) for x in group]
    seen: set[tuple[str, str]] = set()
    for suggestion in r.suggestions:  # type: ignore[attr-defined]
        for side_record in (suggestion.left, suggestion.right):
            key = (side_record.source_code, side_record.reference)
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


# ---------------------------------------------------------------------------
# The partition
# ---------------------------------------------------------------------------


def test_partition_is_total_and_disjoint() -> None:
    """Every record in, exactly one bucket out. This is what makes AC2 true."""
    left = [
        rec("T-1"),  # pairs
        rec("T-2", status=RecordStatus.CANCELLED),  # excluded
        rec("T-3"),  # unmatched
        rec("T-4"),  # cancelled on the other side
        rec("T-5", instrument="ETH-USD", quantity="9.00", minutes=5),  # suggestion
    ]
    rights = [
        right("T-1"),
        right("T-2", status=RecordStatus.CANCELLED),
        right("T-4", status=RecordStatus.CANCELLED),
        right("C-9", instrument="ETH-USD", quantity="9.00", minutes=17),
        right("T-8"),  # unmatched the other way
    ]
    result = match(left, rights, TOL)
    appearances = every_record(result)

    assert len(appearances) == len(set(appearances)), "a record appeared in two buckets"
    assert set(appearances) == {(r.source_code, r.reference) for r in [*left, *rights]}


def test_empty_run_is_not_an_error() -> None:
    result = match([], [], TOL)
    assert result.pairs == ()
    assert result.unmatched_left == ()


# ---------------------------------------------------------------------------
# Tier 2 - reference
# ---------------------------------------------------------------------------


def test_reference_match() -> None:
    result = match([rec("T-1")], [right("T-1")], TOL)
    assert len(result.pairs) == 1
    assert result.pairs[0].origin is PairOrigin.REFERENCE


def test_reference_match_is_exact_not_fuzzy() -> None:
    """Ingestion already normalised; matching does not trim or case-fold.

    The two records are otherwise identical, so they still surface - but as a
    suggestion for a person to confirm, never as an automatic pair. That is the
    whole point of D4: the system says "these look like the same trade", not
    "these are the same trade".
    """
    result = match([rec("T-1")], [right("t-1")], TOL)
    assert result.pairs == ()
    assert len(result.suggestions) == 1
    assert result.suggestions[0].right.reference == "t-1"


def test_both_directions() -> None:
    """Unmatched is reported on both sides, not only the counterparty's.

    Different instruments, so neither is a plausible counterpart for the other
    and each is genuinely unmatched rather than merely unconfirmed.
    """
    result = match([rec("T-1", instrument="BTC-USD")], [right("C-9", instrument="ETH-USD")], TOL)
    assert [r.reference for r in result.unmatched_left] == ["T-1"]
    assert [r.reference for r in result.unmatched_right] == ["C-9"]


def test_one_to_one() -> None:
    """A record participates in at most one pair, even with a duplicate reference."""
    result = match([rec("T-1")], [right("T-1"), right("T-1", row_no=3)], TOL)
    assert len(result.pairs) == 1
    left_ids = [id(p.left) for p in result.pairs]
    assert len(left_ids) == len(set(left_ids))


# ---------------------------------------------------------------------------
# Tier 1 - exclusion
# ---------------------------------------------------------------------------


def test_cancelled_excluded() -> None:
    """A cancelled trade is set aside, and is never reported as unmatched."""
    left = [rec("T-1", status=RecordStatus.CANCELLED)]
    result = match(left, [right("T-1", status=RecordStatus.CANCELLED)], TOL)
    assert [r.reference for r in result.excluded_left] == ["T-1"]
    assert result.pairs == ()
    assert result.unmatched_left == ()
    assert result.suggestions == ()


def test_cancelled_one_side() -> None:
    """Cancelled by one party and not the other is a real break, not a missing row."""
    result = match(
        [rec("T-1", status=RecordStatus.CANCELLED)],
        [right("T-1")],
        TOL,
    )
    assert [r.reference for r in result.status_disagreements] == ["T-1"]
    assert [r.source_code for r in result.status_disagreements] == ["statement"]
    assert [r.reference for r in result.excluded_left] == ["T-1"]
    assert result.unmatched_right == ()


# ---------------------------------------------------------------------------
# Tier 3 - suggestions
# ---------------------------------------------------------------------------


def test_suggestions() -> None:
    result = match([rec("T-1")], [right("C-9", minutes=30)], TOL)
    assert len(result.suggestions) == 1
    suggestion = result.suggestions[0]
    assert suggestion.left.reference == "T-1"
    assert suggestion.right.reference == "C-9"
    assert suggestion.rank == 1
    assert suggestion.time_gap_seconds == Decimal(1800)
    assert "instrument" in suggestion.reason


def test_no_auto_apply() -> None:
    """A suggestion is a question. It never becomes a pair on its own (D4)."""
    result = match([rec("T-1")], [right("C-9", minutes=30)], TOL)
    assert result.pairs == ()
    assert result.unmatched_left == ()  # its state is "suggested", not "unmatched"
    assert result.unmatched_right == ()


def test_suggestion_window_is_wider_than_the_comparison_tolerance() -> None:
    """A suggestion is a question and a comparison is an assertion (D5)."""
    inside = match([rec("T-1")], [right("C-9", minutes=110)], TOL)
    outside = match([rec("T-1")], [right("C-9", minutes=130)], TOL)
    assert len(inside.suggestions) == 1
    assert outside.suggestions == ()
    assert len(outside.unmatched_left) == 1


def test_candidates_need_the_same_instrument_and_side() -> None:
    wrong_instrument = match([rec("T-1")], [right("C-9", instrument="ETH-USD")], TOL)
    wrong_side = match([rec("T-1")], [right("C-9", side=Side.SELL)], TOL)
    assert wrong_instrument.suggestions == ()
    assert wrong_side.suggestions == ()


def test_candidates_are_ranked_closest_first() -> None:
    result = match(
        [rec("T-1")],
        [right("C-9", minutes=60), right("C-8", minutes=10), right("C-7", minutes=30)],
        TOL,
    )
    assert [s.right.reference for s in result.suggestions] == ["C-8", "C-7", "C-9"]
    assert [s.rank for s in result.suggestions] == [1, 2, 3]


def test_rejected_not_resuggested() -> None:
    """A pair a person rejected is never offered again (R4.6)."""
    rejection = PriorResolution(
        kind=ResolutionKind.REJECT_SUGGESTION,
        left=RecordKey("ledger", "T-1"),
        right=RecordKey("statement", "C-9"),
    )
    without = match([rec("T-1")], [right("C-9", minutes=30)], TOL)
    with_rejection = match([rec("T-1")], [right("C-9", minutes=30)], TOL, [rejection])
    assert len(without.suggestions) == 1
    assert with_rejection.suggestions == ()
    assert len(with_rejection.unmatched_left) == 1


def test_candidate_search_does_not_compare_every_pair(monkeypatch) -> None:
    """TR-314. Bucketing by instrument and side, not a cross-product."""
    calls = 0
    original = match_module.compare_quantity

    def counting(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(match_module, "compare_quantity", counting)

    instruments = [f"SYM-{n}" for n in range(8)]
    buckets = len(instruments) * 2

    def spread(prefix: str, factory):
        return [
            factory(
                f"{prefix}-{i}",
                instrument=instruments[i % len(instruments)],
                side=Side.BUY if (i // len(instruments)) % 2 else Side.SELL,
            )
            for i in range(200)
        ]

    left = spread("T", rec)
    rights = spread("C", right)
    match(left, rights, TOL)

    cross_product = len(left) * len(rights)
    # Bucketing costs one comparison per left record against its own bucket
    # only. Buckets divide unevenly, so allow one extra per left record rather
    # than pinning the assertion to an exact quotient.
    ideal = cross_product // buckets
    assert calls <= ideal + len(left), (
        f"{calls} comparisons against a {cross_product} cross-product over "
        f"{buckets} buckets (ideal {ideal}) - candidate search is not bucketing"
    )


# ---------------------------------------------------------------------------
# Tier 0 - carried-forward decisions
# ---------------------------------------------------------------------------


def test_carry_forward_first() -> None:
    """Stored decisions are applied before anything automatic (R4.1)."""
    manual = PriorResolution(
        kind=ResolutionKind.MANUAL_MATCH,
        left=RecordKey("ledger", "T-1"),
        right=RecordKey("statement", "C-9"),
    )
    result = match([rec("T-1")], [right("C-9", minutes=30)], TOL, [manual])
    assert len(result.pairs) == 1
    assert result.pairs[0].origin is PairOrigin.MANUAL
    assert result.suggestions == ()


def test_manual_pair_compared() -> None:
    """Pairing asserts identity, not agreement.

    A manual pair whose values disagree is still returned as a pair, so the
    comparison step can report it as a break. Hiding it would make the manual
    match a way of silencing a finding (TR-316).
    """
    manual = PriorResolution(
        kind=ResolutionKind.MANUAL_MATCH,
        left=RecordKey("ledger", "T-1"),
        right=RecordKey("statement", "C-9"),
    )
    result = match(
        [rec("T-1", gross_amount="62000.00")],
        [right("C-9", gross_amount="99999.00", minutes=30)],
        TOL,
        [manual],
    )
    assert len(result.pairs) == 1
    pair = result.pairs[0]
    assert pair.left.gross_amount != pair.right.gross_amount


def test_a_resolution_naming_an_absent_record_is_inert() -> None:
    """The file may simply not have arrived. That is not an error."""
    manual = PriorResolution(
        kind=ResolutionKind.MANUAL_MATCH,
        left=RecordKey("ledger", "T-404"),
        right=RecordKey("statement", "C-404"),
    )
    result = match([rec("T-1")], [right("T-1")], TOL, [manual])
    assert len(result.pairs) == 1
    assert result.pairs[0].origin is PairOrigin.REFERENCE


def test_accepted_unmatched_stays_out_of_the_worklist() -> None:
    accepted = PriorResolution(
        kind=ResolutionKind.ACCEPT_UNMATCHED, left=RecordKey("ledger", "T-1")
    )
    result = match([rec("T-1")], [], TOL, [accepted])
    assert [r.reference for r in result.accepted_unmatched] == ["T-1"]
    assert result.unmatched_left == ()


def test_auto_revoke() -> None:
    """The only automatic reversal in the system, and never a silent one.

    An acceptance was an honest decision on incomplete information. When the
    correction supplies the counterpart, keeping it would hide exactly what the
    correction fixed (R7.7, D10).
    """
    accepted = PriorResolution(
        kind=ResolutionKind.ACCEPT_UNMATCHED, left=RecordKey("ledger", "T-1")
    )
    result = match([rec("T-1")], [right("T-1")], TOL, [accepted])

    assert len(result.pairs) == 1
    assert result.accepted_unmatched == ()
    assert len(result.revocations) == 1
    assert result.revocations[0].key == RecordKey("ledger", "T-1")
    assert "counterpart" in result.revocations[0].reason


def test_a_suggestion_alone_does_not_revoke_an_acceptance() -> None:
    """Only a genuine counterpart revokes. A suggestion is not one (R7.7)."""
    accepted = PriorResolution(
        kind=ResolutionKind.ACCEPT_UNMATCHED, left=RecordKey("ledger", "T-1")
    )
    result = match([rec("T-1")], [right("C-9", minutes=30)], TOL, [accepted])
    assert result.revocations == ()
    assert [r.reference for r in result.accepted_unmatched] == ["T-1"]


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_deterministic() -> None:
    """Same inputs, identical output, including order (TR-303)."""
    left = [rec(f"T-{i}", minutes=i) for i in range(10)]
    rights = [right(f"T-{i}", minutes=i) for i in range(3, 13)]

    first = match(left, rights, TOL)
    shuffled = match(list(reversed(left)), list(reversed(rights)), TOL)

    assert [(p.left.reference, p.right.reference) for p in first.pairs] == [
        (p.left.reference, p.right.reference) for p in shuffled.pairs
    ]
    assert [r.reference for r in first.unmatched_left] == [
        r.reference for r in shuffled.unmatched_left
    ]


def test_equally_ranked_candidates_break_ties_stably() -> None:
    """Two candidates the same distance away must not swap between runs."""
    left = [rec("T-1", minutes=60)]
    rights = [right("C-2", minutes=30), right("C-1", minutes=90)]
    order = [s.right.reference for s in match(left, rights, TOL).suggestions]
    assert order == [
        s.right.reference for s in match(left, list(reversed(rights)), TOL).suggestions
    ]
    assert order == ["C-1", "C-2"]
