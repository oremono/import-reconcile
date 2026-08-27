"""Field-by-field comparison of one pair.

`compare` answers three questions about a pair, in one pass and without
choosing a winner (D12, TR-410): which fields differ, whether each difference
is inside its tolerance, and what the pair's verdict therefore is.

A ``FieldDiff`` is emitted for *every* compared field, including the ones that
agree, so the detail page renders the whole record without a second pass
(TR-401). Every differing field is reported - comparison never stops at the
first (TR-402, R5.3).

The thresholds arrive in ``Tolerances``; none is written down here (TR-405).
The arithmetic lives in :mod:`core.tolerance`, including its sign convention:
a difference is ``right - left``, so a positive difference means the right-hand
side is higher.

Standard library only. See CLAUDE.md invariant 2.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from core.model import (
    COMPARED_FIELDS,
    Comparison,
    FieldDiff,
    NormalizedRecord,
    Tolerances,
    Verdict,
)
from core.tolerance import compare_amount, compare_price, compare_quantity, compare_time

ZERO = Decimal(0)

_Predicate = Callable[[Decimal, Decimal, Tolerances], tuple[bool, Decimal, Decimal]]
_Builder = Callable[[NormalizedRecord, NormalizedRecord, Tolerances], FieldDiff]


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def format_decimal(value: Decimal) -> str:
    """Plain notation, at the precision received. Never scientific.

    ``str(Decimal("0.00000001"))`` is fine, but ``str(Decimal("1E+3"))`` is
    ``'1E+3'``, which is not a number an analyst wants to read in a table.
    """
    return format(value, "f")


def format_timestamp(value: datetime) -> str:
    """ISO-8601 in UTC. Every stored timestamp is already UTC (CLAUDE.md invariant 6)."""
    return value.astimezone(UTC).isoformat()


# ---------------------------------------------------------------------------
# Per-field comparison
# ---------------------------------------------------------------------------


def _exact(field: str, left: str, right: str) -> FieldDiff:
    """Exact equality, no tolerance (TR-408).

    Instrument, side, and status carry no magnitude: a difference is not small
    or large, it is a different trade, a different direction, or a real
    finding. ``abs_diff`` and ``rel_diff`` stay ``None``.
    """
    differs = left != right
    return FieldDiff(
        field=field,
        left_value=left,
        right_value=right,
        differs=differs,
        within_tolerance=not differs,
    )


def _numeric(
    field: str, left: Decimal, right: Decimal, tol: Tolerances, rule: _Predicate
) -> FieldDiff:
    within, abs_diff, rel_diff = rule(left, right, tol)
    return FieldDiff(
        field=field,
        left_value=format_decimal(left),
        right_value=format_decimal(right),
        differs=abs_diff != ZERO,
        within_tolerance=within,
        abs_diff=abs_diff,
        rel_diff=rel_diff,
    )


def _occurred_at(left: NormalizedRecord, right: NormalizedRecord, tol: Tolerances) -> FieldDiff:
    within, abs_diff, _ = compare_time(left.occurred_at, right.occurred_at, tol)
    return FieldDiff(
        field="occurred_at",
        left_value=format_timestamp(left.occurred_at),
        right_value=format_timestamp(right.occurred_at),
        differs=abs_diff != ZERO,
        within_tolerance=within,
        # Seconds, the field's own units. A relative difference between two
        # timestamps means nothing, so none is recorded (R5.4).
        abs_diff=abs_diff,
        rel_diff=None,
    )


def _instrument(left: NormalizedRecord, right: NormalizedRecord, tol: Tolerances) -> FieldDiff:
    return _exact("instrument", left.instrument, right.instrument)


def _side(left: NormalizedRecord, right: NormalizedRecord, tol: Tolerances) -> FieldDiff:
    return _exact("side", str(left.side), str(right.side))


def _status(left: NormalizedRecord, right: NormalizedRecord, tol: Tolerances) -> FieldDiff:
    return _exact("status", str(left.status), str(right.status))


def _quantity(left: NormalizedRecord, right: NormalizedRecord, tol: Tolerances) -> FieldDiff:
    return _numeric("quantity", left.quantity, right.quantity, tol, compare_quantity)


def _unit_price(left: NormalizedRecord, right: NormalizedRecord, tol: Tolerances) -> FieldDiff:
    return _numeric("unit_price", left.unit_price, right.unit_price, tol, compare_price)


def _gross_amount(left: NormalizedRecord, right: NormalizedRecord, tol: Tolerances) -> FieldDiff:
    return _numeric("gross_amount", left.gross_amount, right.gross_amount, tol, compare_amount)


_BUILDERS: dict[str, _Builder] = {
    "occurred_at": _occurred_at,
    "instrument": _instrument,
    "side": _side,
    "quantity": _quantity,
    "unit_price": _unit_price,
    "gross_amount": _gross_amount,
    "status": _status,
}


# ---------------------------------------------------------------------------
# The pair
# ---------------------------------------------------------------------------


def verdict_for(diffs: tuple[FieldDiff, ...]) -> Verdict:
    """TR-409, R5.1, R5.2.

    Anything outside tolerance is a break. Otherwise a pair that differs at all,
    however slightly, is agreed *with drift* - visible to an analyst who goes
    looking, but out of the worklist.
    """
    if any(not d.within_tolerance for d in diffs):
        return Verdict.BREAK
    if any(d.differs for d in diffs):
        return Verdict.AGREED_WITH_DRIFT
    return Verdict.AGREED


def compare(left: NormalizedRecord, right: NormalizedRecord, tol: Tolerances) -> Comparison:
    """Compare a pair field by field. Pure; no IO, no database, no ordering bias.

    The output carries both values and a signed difference for every compared
    field, in ``COMPARED_FIELDS`` order, and names no authoritative side.
    """
    diffs = tuple(_BUILDERS[field](left, right, tol) for field in COMPARED_FIELDS)
    return Comparison(verdict=verdict_for(diffs), diffs=diffs)
