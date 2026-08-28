"""Field-by-field comparison. TR-401..TR-403, TR-405, TR-408..TR-410, AC5.

The comparison logic is required to be testable with no database and no
browser, so this module builds records by hand and imports nothing but `core`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from core.compare import compare, format_decimal, format_duration, format_timestamp
from core.model import (
    COMPARED_FIELDS,
    Comparison,
    NormalizedRecord,
    RecordStatus,
    Side,
    Verdict,
)
from core.tolerance import tolerances_from_config

PROFILE: dict[str, str] = {
    "amount_bps": "0.0005",  # 5 bps
    "amount_abs_floor": "0.01",
    "price_bps": "0.0005",  # 5 bps
    "qty_bps": "0.0001",  # 1 bp
    "time_tolerance_seconds": "300",  # 5 minutes
    "suggest_window_seconds": "7200",
}

TOL = tolerances_from_config(PROFILE)

AT = datetime(2025, 7, 1, 9, 15, 0, tzinfo=UTC)


def d(text: str) -> Decimal:
    return Decimal(text)


def record(source_code: str = "LEDGER", **overrides: Any) -> NormalizedRecord:
    """A record that agrees with its counterpart unless the test says otherwise."""
    fields: dict[str, Any] = {
        "source_code": source_code,
        "reference": "TRD-001",
        "occurred_at": AT,
        "instrument": "ACME",
        "side": Side.BUY,
        "quantity": d("100"),
        "unit_price": d("340.00"),
        "gross_amount": d("34000.00"),
        "status": RecordStatus.SETTLED,
        "row_no": 2,
        "raw": {},
    }
    fields.update(overrides)
    return NormalizedRecord(**fields)


def pair(**right_overrides: Any) -> Comparison:
    return compare(record(), record("STATEMENT", **right_overrides), TOL)


def by_field(result: Comparison) -> dict[str, Any]:
    return {diff.field: diff for diff in result.diffs}


# ---------------------------------------------------------------------------
# TR-401 - every field, including the ones that agree
# ---------------------------------------------------------------------------


def test_all_fields_emitted() -> None:
    """The detail page renders the whole record in one pass, so agreement is emitted too."""
    result = pair(gross_amount=d("34170.00"))

    assert tuple(diff.field for diff in result.diffs) == COMPARED_FIELDS
    assert len(result.diffs) == len(COMPARED_FIELDS)

    agreeing = [diff for diff in result.diffs if not diff.differs]
    assert len(agreeing) == len(COMPARED_FIELDS) - 1
    for diff in agreeing:
        # An agreeing field still carries both values - the page needs them.
        assert diff.left_value == diff.right_value
        assert diff.left_value != ""
        assert diff.within_tolerance

    fields = by_field(result)
    assert fields["instrument"].left_value == "ACME"
    assert fields["side"].left_value == "BUY"
    assert fields["status"].left_value == "SETTLED"
    assert fields["quantity"].left_value == "100"
    assert fields["occurred_at"].left_value == "2025-07-01 09:15:00 UTC"


def test_agreement_still_emits_every_field() -> None:
    result = pair()
    assert len(result.diffs) == len(COMPARED_FIELDS)
    assert result.differing == ()
    assert result.out_of_tolerance == ()


# ---------------------------------------------------------------------------
# TR-402 - every difference, not the first
# ---------------------------------------------------------------------------


def test_all_diffs() -> None:
    """AC5: a price difference plus an amount difference tell a different story."""
    result = pair(
        quantity=d("101"),
        unit_price=d("341.00"),
        gross_amount=d("34441.00"),
    )

    differing = {diff.field for diff in result.differing}
    assert differing == {"quantity", "unit_price", "gross_amount"}
    assert {diff.field for diff in result.out_of_tolerance} == differing
    assert result.verdict is Verdict.BREAK

    # Comparison did not stop at the first field: the later ones carry magnitudes.
    fields = by_field(result)
    assert fields["quantity"].abs_diff == Decimal(1)
    assert fields["unit_price"].abs_diff == d("1.00")
    assert fields["gross_amount"].abs_diff == d("441.00")


def test_differences_of_every_kind_are_reported_together() -> None:
    result = pair(
        occurred_at=AT + timedelta(minutes=40),
        instrument="ACMX",
        side=Side.SELL,
        status=RecordStatus.PENDING,
        gross_amount=d("34170.00"),
    )
    assert {diff.field for diff in result.differing} == {
        "occurred_at",
        "instrument",
        "side",
        "status",
        "gross_amount",
    }
    assert result.verdict is Verdict.BREAK


# ---------------------------------------------------------------------------
# TR-403 / TR-410 - symmetry, and no authoritative side
# ---------------------------------------------------------------------------


def test_symmetry() -> None:
    """Swapping the sides changes the sign of each difference and nothing else."""
    left = record()
    right = record(
        "STATEMENT",
        occurred_at=AT + timedelta(seconds=120),
        quantity=d("100.005"),
        unit_price=d("341.00"),
        gross_amount=d("34170.00"),
    )

    forward = compare(left, right, TOL)
    backward = compare(right, left, TOL)

    assert forward.verdict is backward.verdict
    assert tuple(diff.field for diff in forward.diffs) == tuple(
        diff.field for diff in backward.diffs
    )

    for ahead, behind in zip(forward.diffs, backward.diffs, strict=True):
        assert ahead.differs is behind.differs
        assert ahead.within_tolerance is behind.within_tolerance
        assert ahead.left_value == behind.right_value
        assert ahead.right_value == behind.left_value
        assert ahead.rel_diff == behind.rel_diff  # a magnitude, not a direction
        if ahead.abs_diff is None:
            assert behind.abs_diff is None
        else:
            assert behind.abs_diff is not None
            assert ahead.abs_diff == -behind.abs_diff

    assert forward.max_rel_diff == backward.max_rel_diff


def test_relative_difference_uses_the_larger_magnitude() -> None:
    """Not the left value, not "ours" - the larger of the two (TR-403)."""
    result = pair(gross_amount=d("34170.00"))
    amount = by_field(result)["gross_amount"]

    assert amount.abs_diff == d("170.00")
    assert amount.rel_diff == d("170.00") / d("34170.00")
    assert amount.rel_diff != d("170.00") / d("34000.00")


def test_no_authoritative_side() -> None:
    """D12: the output carries both values and a signed difference, never a winner."""
    left = record(gross_amount=d("34000.00"))
    right = record("STATEMENT", gross_amount=d("34170.00"))

    result = compare(left, right, TOL)
    amount = by_field(result)["gross_amount"]

    # Both values survive into the output, labelled by position only.
    assert amount.left_value == "34000.00"
    assert amount.right_value == "34170.00"

    # The sign is the whole verdict on direction: positive means the right side
    # is higher. There is no "correct", "expected", or "authoritative" value.
    assert amount.abs_diff == d("170.00")
    assert by_field(compare(right, left, TOL))["gross_amount"].abs_diff == d("-170.00")

    field_names = set(amount.__slots__)
    assert not field_names & {"expected", "actual", "correct", "authoritative", "master"}

    # Neither source code reaches the comparison output at all.
    rendered = [(diff.left_value, diff.right_value) for diff in result.diffs]
    assert not any("LEDGER" in str(v) or "STATEMENT" in str(v) for cell in rendered for v in cell)


# ---------------------------------------------------------------------------
# TR-405 - thresholds come from the profile
# ---------------------------------------------------------------------------


def test_tolerances_from_profile() -> None:
    """The same pair is a break or a drift depending only on the profile."""
    left = record()
    right = record("STATEMENT", gross_amount=d("34010.00"))  # 10.00 apart

    strict = tolerances_from_config({**PROFILE, "amount_bps": "0.0001"})  # 1 bp -> 3.40
    generous = tolerances_from_config({**PROFILE, "amount_bps": "0.005"})  # 50 bps -> 170.05

    assert compare(left, right, strict).verdict is Verdict.BREAK
    assert compare(left, right, generous).verdict is Verdict.AGREED_WITH_DRIFT

    # Time is configured independently of amount.
    late = record("STATEMENT", occurred_at=AT + timedelta(minutes=6))
    assert compare(left, late, TOL).verdict is Verdict.BREAK
    wider = tolerances_from_config({**PROFILE, "time_tolerance_seconds": "600"})
    assert compare(left, late, wider).verdict is Verdict.AGREED_WITH_DRIFT


# ---------------------------------------------------------------------------
# TR-408 - exact fields
# ---------------------------------------------------------------------------


def test_exact_fields() -> None:
    """Instrument, side, and status compare by equality. No tolerance applies."""
    for field, value in (
        ("instrument", "ACMX"),
        ("side", Side.SELL),
        ("status", RecordStatus.PENDING),
    ):
        result = pair(**{field: value})
        diff = by_field(result)[field]

        assert diff.differs
        assert not diff.within_tolerance
        assert diff.abs_diff is None  # a difference here has no magnitude
        assert diff.rel_diff is None
        assert result.verdict is Verdict.BREAK
        assert result.differing == (diff,)


def test_exact_fields_carry_no_magnitude_when_they_agree() -> None:
    result = pair()
    for field in ("instrument", "side", "status"):
        diff = by_field(result)[field]
        assert not diff.differs
        assert diff.within_tolerance
        assert diff.abs_diff is None
        assert diff.rel_diff is None


def test_exact_fields_compare_after_vocabulary_mapping() -> None:
    """Both sides are already in our vocabulary, so the enum value is what is compared."""
    result = compare(record(side=Side.BUY), record("STATEMENT", side=Side.BUY), TOL)
    side = by_field(result)["side"]
    assert (side.left_value, side.right_value) == ("BUY", "BUY")
    assert not side.differs


# ---------------------------------------------------------------------------
# TR-409 - the three verdicts
# ---------------------------------------------------------------------------


def test_verdicts() -> None:
    # Identical in every field: agreed, no attention needed.
    assert pair().verdict is Verdict.AGREED

    # Differs, but every field is inside its tolerance: drift, out of the worklist.
    drift = pair(
        quantity=d("100.005"),  # 1 bp of 100.005 is 0.0100005
        unit_price=d("340.10"),  # 5 bps of 340.10 is 0.170050
        gross_amount=d("34010.00"),  # 5 bps of 34010.00 is 17.005
        occurred_at=AT + timedelta(seconds=120),
    )
    assert drift.verdict is Verdict.AGREED_WITH_DRIFT
    assert drift.differing != ()
    assert drift.out_of_tolerance == ()

    # One field outside tolerance is enough.
    assert pair(gross_amount=d("34170.00")).verdict is Verdict.BREAK


def test_verdict_agreed_requires_no_difference_at_all() -> None:
    """R5.1: "if any field differed at all, however slightly"."""
    hair = pair(gross_amount=d("34000.001"))
    assert hair.out_of_tolerance == ()
    assert hair.verdict is Verdict.AGREED_WITH_DRIFT


def test_worked_example_amount_break() -> None:
    """D1: 34,000.00 against 34,170.00 is ten times the threshold."""
    result = pair(gross_amount=d("34170.00"))
    assert result.verdict is Verdict.BREAK
    assert {diff.field for diff in result.out_of_tolerance} == {"gross_amount"}


def test_worked_example_same_timestamp_agrees() -> None:
    """D2: 09:15:00 against 09:15:00."""
    same = datetime(2025, 7, 1, 9, 15, 0, tzinfo=UTC)
    result = compare(record(occurred_at=same), record("STATEMENT", occurred_at=same), TOL)
    assert result.verdict is Verdict.AGREED
    assert by_field(result)["occurred_at"].abs_diff == Decimal(0)


def test_worked_example_forty_minute_gap_breaks() -> None:
    """D2: 10:00 against 10:40 is 2400 seconds."""
    ten = datetime(2025, 7, 1, 10, 0, tzinfo=UTC)
    result = compare(
        record(occurred_at=ten),
        record("STATEMENT", occurred_at=ten + timedelta(minutes=40)),
        TOL,
    )
    assert result.verdict is Verdict.BREAK
    occurred = by_field(result)["occurred_at"]
    assert occurred.abs_diff == Decimal(2400)  # seconds, the field's own units
    assert occurred.rel_diff is None  # meaningless between two timestamps


def test_worked_example_quantity_formatting_agrees() -> None:
    """D3: 0.50 against 0.5 is formatting - equal, and therefore not even drift."""
    result = compare(
        record(quantity=d("0.50")),
        record("STATEMENT", quantity=d("0.5")),
        TOL,
    )
    quantity = by_field(result)["quantity"]
    assert (quantity.left_value, quantity.right_value) == ("0.50", "0.5")
    assert not quantity.differs
    assert quantity.abs_diff == Decimal(0)
    assert result.verdict is Verdict.AGREED


def test_max_rel_diff_sorts_on_the_largest_relative_difference() -> None:
    result = pair(unit_price=d("341.00"), gross_amount=d("34010.00"))
    price = by_field(result)["unit_price"]
    assert result.max_rel_diff == price.rel_diff


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def test_decimals_render_in_plain_notation() -> None:
    """Never scientific: an analyst reading a table should not meet 1E+3."""
    assert format_decimal(Decimal("1E+3")) == "1000"
    assert format_decimal(Decimal("0.00000001")) == "0.00000001"
    assert format_decimal(Decimal("-170.00")) == "-170.00"

    big = pair(gross_amount=Decimal("3.417E+4"))
    assert by_field(big)["gross_amount"].right_value == "34170"
    assert "E" not in by_field(big)["gross_amount"].right_value


def test_precision_received_is_the_precision_displayed() -> None:
    result = compare(record(quantity=d("100.000")), record("STATEMENT", quantity=d("100")), TOL)
    quantity = by_field(result)["quantity"]
    assert (quantity.left_value, quantity.right_value) == ("100.000", "100")


def test_timestamps_render_readably_in_utc() -> None:
    """Every stored timestamp is UTC, so the offset is the same on every row.

    Printing it anyway costs six characters on each of two columns and tells
    the reader nothing. Seconds stay, because two bookings thirty seconds apart
    must not render identically.
    """
    ist = timezone(timedelta(hours=5, minutes=30))
    local = datetime(2025, 7, 1, 14, 45, 0, tzinfo=ist)  # 09:15 UTC
    assert format_timestamp(local) == "2025-07-01 09:15:00 UTC"

    result = compare(record(), record("STATEMENT", occurred_at=local), TOL)
    occurred = by_field(result)["occurred_at"]
    assert occurred.left_value == occurred.right_value == "2025-07-01 09:15:00 UTC"
    assert not occurred.differs


def test_a_duration_reads_in_the_largest_unit_that_stays_true() -> None:
    """The stored value keeps every digit; this is only how it is shown."""
    assert format_duration(Decimal("2400.000000000000")) == "40 min"
    assert format_duration(Decimal(0)) == "0 s"
    assert format_duration(Decimal("0.5")) == "0.5 s"
    assert format_duration(Decimal(45)) == "45 s"
    assert format_duration(Decimal(60)) == "1 min"
    assert format_duration(Decimal(90)) == "1 min 30 s"
    assert format_duration(Decimal(3600)) == "1 h"
    assert format_duration(Decimal(7500)) == "2 h 5 min"
    assert format_duration(Decimal(86400)) == "1 d"
    assert format_duration(Decimal(90000)) == "1 d 1 h"


def test_a_duration_is_signed_only_by_its_field_not_its_text() -> None:
    """Direction is carried by ``abs_diff``; the rendered size is a magnitude."""
    assert format_duration(Decimal(-2400)) == format_duration(Decimal(2400))


def test_no_rendered_duration_carries_storage_padding() -> None:
    """The defect this replaced: 2400.000000000000 s, shown next to money."""
    for seconds in (0, 1, 59, 60, 61, 599, 600, 3599, 3600, 86399, 86400):
        rendered = format_duration(Decimal(seconds).quantize(Decimal("0.000000000001")))
        assert "000000" not in rendered, rendered
