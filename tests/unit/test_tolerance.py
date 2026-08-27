"""Tolerance rules: boundary triples for every threshold. TR-405..TR-407, TR-411, AC4.

Every rule gets three cases - just inside the threshold, exactly on it, and the
first representable unit beyond it - because "<= or <" is the ambiguity that
produces a defect nobody notices for a year (TR-406). The values are the ones
SPEC.md section 5.5 states: amount 5 bps with a 0.01 floor, price 5 bps,
quantity 1 bp, time 5 minutes.

No database, no app, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.model import Tolerances
from core.tolerance import (
    DECIMAL_THRESHOLDS,
    INT_THRESHOLDS,
    ToleranceConfigError,
    compare_amount,
    compare_price,
    compare_quantity,
    compare_time,
    difference,
    seconds_between,
    tolerances_from_config,
)

# The profile as it is stored: text, per source pair. SPEC.md section 5.5, D1, D2, D3.
PROFILE: dict[str, str] = {
    "amount_bps": "0.0005",  # 5 bps
    "amount_abs_floor": "0.01",
    "price_bps": "0.0005",  # 5 bps
    "qty_bps": "0.0001",  # 1 bp
    "time_tolerance_seconds": "300",  # 5 minutes
    "suggest_window_seconds": "7200",  # 2 hours
}

TOL = tolerances_from_config(PROFILE)

AT = datetime(2025, 7, 1, 9, 15, 0, tzinfo=UTC)


def d(text: str) -> Decimal:
    return Decimal(text)


# ---------------------------------------------------------------------------
# Parsing the profile. TR-405, TR-411.
# ---------------------------------------------------------------------------


def test_profile_fields() -> None:
    """TR-411: six independent thresholds, each separately configurable."""
    assert TOL.amount_bps == d("0.0005")
    assert TOL.amount_abs_floor == d("0.01")
    assert TOL.price_bps == d("0.0005")
    assert TOL.qty_bps == d("0.0001")
    assert TOL.time_tolerance_seconds == 300
    assert TOL.suggest_window_seconds == 7200

    # Every threshold is a Decimal or an int - nothing arrives as a float.
    for name in DECIMAL_THRESHOLDS:
        assert isinstance(getattr(TOL, name), Decimal)
    for name in INT_THRESHOLDS:
        assert isinstance(getattr(TOL, name), int)

    # They move independently: quantity is tighter than price for a reason (D3).
    tighter = tolerances_from_config({**PROFILE, "qty_bps": "0.00001"})
    assert tighter.qty_bps != TOL.qty_bps
    assert tighter.price_bps == TOL.price_bps
    assert tighter.amount_bps == TOL.amount_bps


@pytest.mark.parametrize("missing", [*DECIMAL_THRESHOLDS, *INT_THRESHOLDS])
def test_missing_threshold_is_a_config_error(missing: str) -> None:
    broken = {k: v for k, v in PROFILE.items() if k != missing}
    with pytest.raises(ToleranceConfigError) as exc:
        tolerances_from_config(broken)
    assert missing in str(exc.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount_bps", "five bps"),
        ("amount_bps", ""),
        ("amount_abs_floor", "NaN"),
        ("price_bps", "Infinity"),
        ("qty_bps", "-0.0001"),
        ("time_tolerance_seconds", "300.5"),
        ("time_tolerance_seconds", "five minutes"),
        ("suggest_window_seconds", "-1"),
    ],
)
def test_unparseable_threshold_is_a_config_error(field: str, value: str) -> None:
    with pytest.raises(ToleranceConfigError):
        tolerances_from_config({**PROFILE, field: value})


def test_config_error_is_a_value_error() -> None:
    """Callers that only know `ValueError` still catch it."""
    assert issubclass(ToleranceConfigError, ValueError)


# ---------------------------------------------------------------------------
# The relative difference. TR-403, TR-410.
# ---------------------------------------------------------------------------


def test_relative_difference_divides_by_the_larger_magnitude() -> None:
    signed, relative, larger = difference(d("100.00"), d("110.00"))
    assert signed == d("10.00")
    assert larger == d("110.00")
    assert relative == d("10.00") / d("110.00")  # not 10/100 - neither side is the reference


def test_swapping_flips_only_the_sign() -> None:
    forward = difference(d("34000.00"), d("34170.00"))
    backward = difference(d("34170.00"), d("34000.00"))
    assert forward[0] == -backward[0]
    assert forward[0] > 0  # right is higher: the sign convention, stated
    assert forward[1] == backward[1]  # magnitude, unchanged
    assert forward[2] == backward[2]


def test_both_zero_does_not_divide_by_zero() -> None:
    within, abs_diff, rel_diff = compare_amount(d("0.00"), d("0.00"), TOL)
    assert (within, abs_diff, rel_diff) == (True, d("0.00"), Decimal(0))


def test_zero_against_nonzero_is_a_whole_difference() -> None:
    within, abs_diff, rel_diff = compare_amount(d("0.00"), d("5.00"), TOL)
    assert not within
    assert abs_diff == d("5.00")
    assert rel_diff == Decimal(1)


# ---------------------------------------------------------------------------
# Amount: floor against relative. TR-407, D1.
# ---------------------------------------------------------------------------


def test_floor_vs_relative() -> None:
    """TR-407: the allowance is the greater of the two, so each rescues the other.

    On a small amount the relative allowance is worthless and the floor decides;
    on a large one the floor is worthless and the relative allowance decides.
    """
    # 10.00: relative allowance is 0.005 - too small. The 0.01 floor allows 0.008.
    assert compare_amount(d("10.000"), d("10.008"), TOL)[0]
    assert not compare_price(d("10.000"), d("10.008"), TOL)[0]  # no floor on price

    # 20000.00: the floor is worthless. The relative allowance (10.00) allows 5.00.
    assert compare_amount(d("20000.00"), d("20005.00"), TOL)[0]
    assert d("5.00") > TOL.amount_abs_floor

    # And the floor never *shrinks* an allowance the relative rule already grants.
    assert compare_amount(d("20000.00"), d("20010.00"), TOL)[0]


def test_amount_boundary_triple_floor_branch() -> None:
    """Small amount: the 0.01 floor is the threshold. Just inside, on, just outside."""
    base = d("10.000")
    assert compare_amount(base, base + d("0.005"), TOL)[0]  # inside
    assert compare_amount(base, base + d("0.010"), TOL)[0]  # exactly on -> within (TR-406)
    assert not compare_amount(base, base + d("0.011"), TOL)[0]  # first unit beyond -> break


def test_amount_boundary_triple_relative_branch() -> None:
    """Large amount: 5 bps of the larger value, which is exactly 10.00 at 20000.00."""
    larger = d("20000.00")
    assert TOL.amount_bps * larger == d("10.000000")
    assert compare_amount(larger - d("9.99"), larger, TOL)[0]  # inside
    assert compare_amount(larger - d("10.00"), larger, TOL)[0]  # exactly on -> within
    assert not compare_amount(larger - d("10.01"), larger, TOL)[0]  # beyond -> break


def test_amount_boundary_is_symmetric_under_swap() -> None:
    left, right = d("19990.00"), d("20000.00")
    assert compare_amount(left, right, TOL)[0] == compare_amount(right, left, TOL)[0]
    assert compare_amount(left, right, TOL)[1] == -compare_amount(right, left, TOL)[1]


def test_amount_worked_example_is_a_break() -> None:
    """D1: 34,000.00 against 34,170.00 is 0.50%, ten times the threshold."""
    within, abs_diff, rel_diff = compare_amount(d("34000.00"), d("34170.00"), TOL)
    assert not within
    assert abs_diff == d("170.00")
    assert rel_diff.quantize(d("0.0001")) == d("0.0050")  # 0.50% of the larger value


# ---------------------------------------------------------------------------
# Price: 5 bps, no floor.
# ---------------------------------------------------------------------------


def test_price_boundary_triple() -> None:
    """5 bps of 100.00 is exactly 0.05."""
    larger = d("100.00")
    assert TOL.price_bps * larger == d("0.050000")
    assert compare_price(larger, larger - d("0.0499"), TOL)[0]  # inside
    assert compare_price(larger, larger - d("0.0500"), TOL)[0]  # exactly on -> within
    assert not compare_price(larger, larger - d("0.0501"), TOL)[0]  # beyond -> break


def test_price_has_no_absolute_floor() -> None:
    """The amount floor is an amount rule. A tiny price is held to 5 bps and no more."""
    assert not compare_price(d("0.0100"), d("0.0101"), TOL)[0]
    assert compare_amount(d("0.0100"), d("0.0101"), TOL)[0]


# ---------------------------------------------------------------------------
# Quantity: 1 bp. D3.
# ---------------------------------------------------------------------------


def test_quantity_boundary_triple() -> None:
    """1 bp of 1000 is exactly 0.1."""
    larger = d("1000.0")
    assert TOL.qty_bps * larger == d("0.10000")
    assert compare_quantity(larger, larger - d("0.09"), TOL)[0]  # inside
    assert compare_quantity(larger, larger - d("0.10"), TOL)[0]  # exactly on -> within
    assert not compare_quantity(larger, larger - d("0.11"), TOL)[0]  # beyond -> break


def test_quantity_is_tighter_than_price() -> None:
    """D3: quantity does not drift for economic reasons. 5 bps of 1000 would pass."""
    assert compare_price(d("1000.0"), d("999.7"), TOL)[0]
    assert not compare_quantity(d("1000.0"), d("999.7"), TOL)[0]


def test_quantity_absorbs_formatting_only() -> None:
    """D3: 0.50 against 0.5 is formatting, and is not even a difference."""
    within, abs_diff, rel_diff = compare_quantity(d("0.50"), d("0.5"), TOL)
    assert within
    assert abs_diff == Decimal(0)
    assert rel_diff == Decimal(0)


# ---------------------------------------------------------------------------
# Time: 300 seconds. D2.
# ---------------------------------------------------------------------------


def test_seconds_between_is_exact_and_signed() -> None:
    later = AT + timedelta(seconds=90, microseconds=500000)
    assert seconds_between(AT, later) == d("90.5")
    assert seconds_between(later, AT) == d("-90.5")
    assert isinstance(seconds_between(AT, later), Decimal)


def test_seconds_between_spans_days() -> None:
    assert seconds_between(AT, AT + timedelta(days=2)) == Decimal(172800)
    assert seconds_between(AT, AT - timedelta(days=2)) == Decimal(-172800)


def test_time_boundary_triple() -> None:
    """The threshold is 300 seconds; the first representable unit beyond it is 1 microsecond."""
    assert compare_time(AT, AT + timedelta(seconds=299), TOL)[0]  # inside
    assert compare_time(AT, AT + timedelta(seconds=300), TOL)[0]  # exactly on -> within (TR-406)
    assert not compare_time(AT, AT + timedelta(seconds=300, microseconds=1), TOL)[0]
    assert not compare_time(AT, AT + timedelta(seconds=301), TOL)[0]


def test_time_boundary_holds_in_both_directions() -> None:
    earlier = AT - timedelta(seconds=300)
    assert compare_time(AT, earlier, TOL)[0]
    assert compare_time(AT, earlier, TOL)[1] == Decimal(-300)
    assert not compare_time(AT, AT - timedelta(seconds=301), TOL)[0]


def test_time_worked_examples() -> None:
    """D2: 09:15 against 09:15 agrees; 10:00 against 10:40 is 2400 seconds and breaks."""
    same = datetime(2025, 7, 1, 9, 15, tzinfo=UTC)
    assert compare_time(same, same, TOL) == (True, Decimal(0), Decimal(0))

    ten = datetime(2025, 7, 1, 10, 0, tzinfo=UTC)
    ten_forty = datetime(2025, 7, 1, 10, 40, tzinfo=UTC)
    within, abs_diff, _ = compare_time(ten, ten_forty, TOL)
    assert not within
    assert abs_diff == Decimal(2400)


def test_time_relative_difference_is_never_reported() -> None:
    """A relative difference between two timestamps has no meaning."""
    assert compare_time(AT, AT + timedelta(seconds=42), TOL)[2] == Decimal(0)


def test_thresholds_come_from_the_profile_not_the_code() -> None:
    """TR-405: widen the profile and the same values pass; nothing is hard-coded."""
    gap = timedelta(seconds=600)
    assert not compare_time(AT, AT + gap, TOL)[0]

    generous = tolerances_from_config({**PROFILE, "time_tolerance_seconds": "900"})
    assert compare_time(AT, AT + gap, generous)[0]

    strict = tolerances_from_config({**PROFILE, "amount_bps": "0", "amount_abs_floor": "0"})
    assert not compare_amount(d("100.00"), d("100.01"), strict)[0]
    assert compare_amount(d("100.00"), d("100.00"), strict)[0]


def test_tolerances_are_immutable() -> None:
    with pytest.raises(AttributeError):
        TOL.amount_bps = d("1")  # type: ignore[misc]
    assert isinstance(TOL, Tolerances)
