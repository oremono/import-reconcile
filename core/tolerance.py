"""Tolerance thresholds and the within-tolerance predicates.

The thresholds themselves are configuration (SPEC.md R5.5, D13): this module
parses a stored ``tolerance_profile`` into :class:`~core.model.Tolerances` and
applies it. No threshold is a literal here - the numbers in SPEC.md section 5.5
live in the profile, not in this file (TR-405).

Two conventions, both deliberate and both load-bearing:

**Sign.** A difference is reported as ``right - left``. A positive difference
means the right-hand side is higher. Neither side is authoritative (D12,
TR-410), so the sign is information, not a judgement: swapping the arguments
flips the sign and changes nothing else. The relative difference is a magnitude
and is therefore never negative - it is what the worklist sorts on, and a
sort key that flips with argument order would not be one.

**Denominator.** The relative difference divides by the larger of the two
magnitudes (TR-403). Dividing by either side's own value would make one side
the reference, which is the thing D12 forbids. When both sides are zero there
is nothing to divide by and the relative difference is zero.

A difference exactly equal to a threshold is *within* tolerance (TR-406): every
comparison below is ``<=``. The first representable unit beyond the threshold
is a break.

Standard library only. See CLAUDE.md invariant 2.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, DecimalException

from core.model import Tolerances

ZERO = Decimal(0)

_SECONDS_PER_DAY = Decimal(86400)
_MICROSECONDS_PER_SECOND = Decimal(1000000)

#: Profile keys parsed as ``Decimal``. Basis points are fractions: 5 bps is 0.0005.
DECIMAL_THRESHOLDS: tuple[str, ...] = (
    "amount_bps",
    "amount_abs_floor",
    "price_bps",
    "qty_bps",
)

#: Profile keys parsed as whole seconds.
INT_THRESHOLDS: tuple[str, ...] = (
    "time_tolerance_seconds",
    "suggest_window_seconds",
)


class ToleranceConfigError(ValueError):
    """A tolerance profile is missing a threshold, or one will not parse.

    Raised at configuration time rather than mid-run: discovering a broken
    profile halfway through a reconciliation is worse than not starting
    (DESIGN.md section 11).
    """


# ---------------------------------------------------------------------------
# Parsing a profile
# ---------------------------------------------------------------------------


def _raw(config: Mapping[str, str], name: str) -> str:
    try:
        value = config[name]
    except KeyError as exc:
        raise ToleranceConfigError(f"tolerance profile is missing {name!r}") from exc
    return str(value)


def _decimal(config: Mapping[str, str], name: str) -> Decimal:
    raw = _raw(config, name)
    try:
        value = Decimal(raw)
    except (DecimalException, ValueError) as exc:
        raise ToleranceConfigError(f"tolerance {name!r} is not a number: {raw!r}") from exc
    if not value.is_finite():
        raise ToleranceConfigError(f"tolerance {name!r} is not finite: {raw!r}")
    if value < ZERO:
        raise ToleranceConfigError(f"tolerance {name!r} is negative: {raw!r}")
    return value


def _int(config: Mapping[str, str], name: str) -> int:
    raw = _raw(config, name)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ToleranceConfigError(f"tolerance {name!r} is not a whole number: {raw!r}") from exc
    if value < 0:
        raise ToleranceConfigError(f"tolerance {name!r} is negative: {raw!r}")
    return value


def tolerances_from_config(config: Mapping[str, str]) -> Tolerances:
    """Build :class:`~core.model.Tolerances` from a stored profile (TR-405, TR-411).

    Each of the six thresholds is independent and separately configurable; all
    six are required. Values arrive as text - from a database row, a settings
    file, or a test - and are parsed exactly, never through ``float``.

    Raises:
        ToleranceConfigError: a threshold is absent, unparseable, or negative.
    """
    return Tolerances(
        amount_bps=_decimal(config, "amount_bps"),
        amount_abs_floor=_decimal(config, "amount_abs_floor"),
        price_bps=_decimal(config, "price_bps"),
        qty_bps=_decimal(config, "qty_bps"),
        time_tolerance_seconds=_int(config, "time_tolerance_seconds"),
        suggest_window_seconds=_int(config, "suggest_window_seconds"),
    )


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


def difference(left: Decimal, right: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """Return ``(signed difference, relative difference, larger magnitude)``.

    The signed difference is ``right - left``; the relative difference is its
    magnitude over the larger of the two magnitudes, and is zero when both
    sides are zero (TR-403).
    """
    signed = right - left
    larger = max(abs(left), abs(right))
    relative = ZERO if larger == ZERO else abs(signed) / larger
    return signed, relative, larger


def _within_bps(left: Decimal, right: Decimal, bps: Decimal) -> tuple[bool, Decimal, Decimal]:
    signed, relative, larger = difference(left, right)
    return abs(signed) <= bps * larger, signed, relative


def compare_amount(left: Decimal, right: Decimal, tol: Tolerances) -> tuple[bool, Decimal, Decimal]:
    """Amount rule: the greater of the absolute floor and the relative allowance (TR-407, D1).

    The floor keeps tiny trades from generating noise; the relative allowance
    covers fee and rounding drift on large ones. Whichever is larger applies.
    """
    signed, relative, larger = difference(left, right)
    allowance = max(tol.amount_abs_floor, tol.amount_bps * larger)
    return abs(signed) <= allowance, signed, relative


def compare_price(left: Decimal, right: Decimal, tol: Tolerances) -> tuple[bool, Decimal, Decimal]:
    """Unit price rule: a basis-point allowance on the larger value (SPEC.md 5.5)."""
    return _within_bps(left, right, tol.price_bps)


def compare_quantity(
    left: Decimal, right: Decimal, tol: Tolerances
) -> tuple[bool, Decimal, Decimal]:
    """Quantity rule: a tighter basis-point allowance (D3). Quantity does not drift."""
    return _within_bps(left, right, tol.qty_bps)


def seconds_between(left: datetime, right: datetime) -> Decimal:
    """Signed ``right - left`` in seconds, exactly, without touching a float.

    ``timedelta.total_seconds()`` returns a float and so cannot be used here.
    """
    delta = right - left
    return (
        Decimal(delta.days) * _SECONDS_PER_DAY
        + Decimal(delta.seconds)
        + Decimal(delta.microseconds) / _MICROSECONDS_PER_SECOND
    )


def compare_time(left: datetime, right: datetime, tol: Tolerances) -> tuple[bool, Decimal, Decimal]:
    """Timestamp rule: a gap in seconds against ``time_tolerance_seconds`` (D2).

    The difference is in seconds - the field's own units. A *relative*
    difference between two timestamps has no meaning, so the third element is
    always zero and :func:`core.compare.compare` records ``None`` for it.
    """
    signed = seconds_between(left, right)
    return abs(signed) <= Decimal(tol.time_tolerance_seconds), signed, ZERO
