"""Normalisation: mapping, timestamps, vocabulary, precision. TR-201..TR-206.

No database, no app, no network - the brief requires this layer to be testable
without any of them, so this module imports only ``core`` and the standard
library (DESIGN.md section 9).

The three fixtures below are built from the real headers in ``data/*.csv``, so a
test that passes here describes files that actually arrive.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from core.format import FormatConfigError, source_format_from_config
from core.model import NormalizedRecord, RecordStatus, RowError, Side, SourceFormat
from core.normalize import HeaderError, normalize_row, normalize_rows, validate_header

ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Fixtures: the three real sources, expressed the way they are stored
# ---------------------------------------------------------------------------

LEDGER_CONFIG: dict[str, Any] = {
    "columns": {
        "reference": "trade_id",
        "occurred_at": "traded_at",
        "instrument": "instrument",
        "side": "side",
        "quantity": "quantity",
        "unit_price": "price",
        "gross_amount": "gross_amount",
        "status": "state",
    },
    "timestamp_formats": ["%Y-%m-%dT%H:%M:%SZ"],
    "timezone": "UTC",
    "side_map": {"BUY": "BUY", "SELL": "SELL"},
    "status_map": {"SETTLED": "SETTLED", "PENDING": "PENDING", "CANCELLED": "CANCELLED"},
}

STATEMENT_CONFIG: dict[str, Any] = {
    "columns": {
        "reference": "reference",
        "occurred_at": "executed_at",
        "instrument": "symbol",
        "side": "direction",
        "quantity": "qty",
        "unit_price": "unit_price",
        "gross_amount": "total",
        "status": "status",
    },
    "timestamp_formats": ["%Y-%m-%d %H:%M:%S"],
    "timezone": "UTC",
    "side_map": {"B": "BUY", "S": "SELL"},
    "status_map": {"SETTLED": "SETTLED", "PENDING": "PENDING", "CANCELLED": "CANCELLED"},
}

VENUE_C_CONFIG: dict[str, Any] = {
    "columns": {
        "reference": "txn_ref",
        "occurred_at": "ts_epoch",
        "instrument": "pair",
        "side": "bs",
        "quantity": "volume",
        "unit_price": "rate",
        "gross_amount": "value",
        "status": "state",
    },
    "timestamp_formats": ["%s"],
    "timezone": "UTC",
    "side_map": {"d": "BUY", "c": "SELL"},
    "status_map": {"OK": "SETTLED", "VOID": "CANCELLED"},
}

LEDGER_ROW = {
    "trade_id": "T-1001",
    "traded_at": "2025-07-01T14:00:00Z",
    "instrument": "ADA-USD",
    "side": "BUY",
    "quantity": "8000.00",
    "price": "0.58",
    "gross_amount": "4640.00",
    "state": "SETTLED",
}

STATEMENT_ROW = {
    "reference": "T-1001",
    "executed_at": "2025-07-01 14:00:00",
    "symbol": "ADA-USD",
    "direction": "B",
    "qty": "8000",
    "unit_price": "0.58",
    "total": "4640.00",
    "status": "SETTLED",
}

VENUE_C_ROW = {
    "txn_ref": "T-1001",
    "ts_epoch": "1751378400",
    "pair": "ADA-USD",
    "bs": "d",
    "volume": "8000.00",
    "rate": "0.58",
    "value": "4640.00",
    "state": "OK",
}


def ledger() -> SourceFormat:
    return source_format_from_config("ledger", LEDGER_CONFIG)


def statement() -> SourceFormat:
    return source_format_from_config("statement", STATEMENT_CONFIG)


def venue_c() -> SourceFormat:
    return source_format_from_config("venue_c", VENUE_C_CONFIG)


def ok(raw: dict[str, str], fmt: SourceFormat, row_no: int = 2) -> NormalizedRecord:
    """Normalise a row that is expected to be good, or fail loudly."""
    result = normalize_row(raw, fmt, row_no)
    assert isinstance(result, NormalizedRecord), getattr(result, "reason", result)
    return result


def bad(raw: dict[str, str], fmt: SourceFormat, row_no: int = 2) -> RowError:
    """Normalise a row that is expected to be rejected, or fail loudly."""
    result = normalize_row(raw, fmt, row_no)
    assert isinstance(result, RowError), f"expected a RowError, got {result}"
    return result


def replace(raw: dict[str, str], **changes: str) -> dict[str, str]:
    return {**raw, **changes}


# ---------------------------------------------------------------------------
# TR-201 - mapping comes from configuration, never from a source name
# ---------------------------------------------------------------------------


def test_column_mapping() -> None:
    """Two sources, two sets of column names, one set of fields. TR-201, AC1."""
    left = ok(LEDGER_ROW, ledger())
    right = ok(STATEMENT_ROW, statement())

    for record in (left, right):
        assert record.reference == "T-1001"
        assert record.instrument == "ADA-USD"
        assert record.side is Side.BUY
        assert record.status is RecordStatus.SETTLED
        assert record.occurred_at == datetime(2025, 7, 1, 14, 0, tzinfo=UTC)
        assert record.unit_price == Decimal("0.58")
        assert record.gross_amount == Decimal("4640.00")

    assert left.source_code == "ledger"
    assert right.source_code == "statement"


def test_a_third_source_needs_only_a_configuration() -> None:
    """TR-207's premise, at this layer: arbitrary column names, no code change."""
    config = {
        **LEDGER_CONFIG,
        "columns": {field: f"col_{field}" for field in LEDGER_CONFIG["columns"]},
    }
    fmt = source_format_from_config("some_new_venue", config)
    raw = {
        "col_reference": "X-1",
        "col_occurred_at": "2025-07-01T14:00:00Z",
        "col_instrument": "BTC-USD",
        "col_side": "SELL",
        "col_quantity": "1.5",
        "col_unit_price": "62000.00",
        "col_gross_amount": "93000.00",
        "col_status": "SETTLED",
    }
    record = ok(raw, fmt)
    assert record.source_code == "some_new_venue"
    assert record.side is Side.SELL
    assert record.quantity == Decimal("1.5")


def test_normalisation_code_names_no_source() -> None:
    """TR-201's second half, checked rather than trusted (CLAUDE.md invariant 8)."""
    offenders = []
    for module in ("core/normalize.py", "core/format.py"):
        text = (ROOT / module).read_text(encoding="utf-8").lower()
        offenders += [f"{module}: {name}" for name in ("ledger", "venue_c") if name in text]
    assert not offenders, f"normalisation must not know a source by name: {offenders}"


def test_raw_row_is_kept_and_snapshotted() -> None:
    """The original text travels with the record, for TR-106 and the detail page."""
    raw = dict(LEDGER_ROW)
    record = ok(raw, ledger())
    raw["quantity"] = "changed after the fact"
    assert record.raw["quantity"] == "8000.00"


# ---------------------------------------------------------------------------
# TR-202 - the declared patterns, in order
# ---------------------------------------------------------------------------


def test_date_formats() -> None:
    """Both real formats and the epoch column all land on the same instant. TR-202."""
    expected = datetime(2025, 7, 1, 14, 0, tzinfo=UTC)
    assert ok(LEDGER_ROW, ledger()).occurred_at == expected
    assert ok(STATEMENT_ROW, statement()).occurred_at == expected
    assert ok(VENUE_C_ROW, venue_c()).occurred_at == expected


def test_first_matching_format_wins() -> None:
    """The list is ordered and the first that parses is used, not the best fit."""
    config = {**STATEMENT_CONFIG, "timestamp_formats": ["%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M"]}
    fmt = source_format_from_config("statement", config)

    assert ok(STATEMENT_ROW, fmt).occurred_at == datetime(2025, 7, 1, 14, 0, tzinfo=UTC)

    later = ok(replace(STATEMENT_ROW, executed_at="02/07/2025 09:15"), fmt)
    assert later.occurred_at == datetime(2025, 7, 2, 9, 15, tzinfo=UTC)


def test_epoch_seconds_parse_exactly() -> None:
    """Venue C sends epoch seconds; no float touches the conversion. TR-202, TR-205."""
    fmt = venue_c()
    assert ok(VENUE_C_ROW, fmt).occurred_at == datetime(2025, 7, 1, 14, 0, tzinfo=UTC)
    assert ok(replace(VENUE_C_ROW, ts_epoch="0"), fmt).occurred_at == datetime(
        1970, 1, 1, tzinfo=UTC
    )
    assert ok(replace(VENUE_C_ROW, ts_epoch="1751378400.5"), fmt).occurred_at == datetime(
        2025, 7, 1, 14, 0, 0, 500_000, tzinfo=UTC
    )
    assert isinstance(bad(replace(VENUE_C_ROW, ts_epoch="not-a-number"), fmt), RowError)


def test_unparseable_date_is_a_row_error() -> None:
    """Exhausting the pattern list rejects the row and says so. TR-202, TR-106."""
    error = bad(replace(LEDGER_ROW, traded_at="not-a-date"), ledger())
    assert error.row_no == 2
    assert "not-a-date" in error.reason
    assert error.raw["trade_id"] == "T-1001"


def test_a_date_in_another_sources_format_is_still_rejected() -> None:
    """A pattern list is a declaration, not a suggestion: no fallback parsing."""
    error = bad(replace(LEDGER_ROW, traded_at="2025-07-01 14:00:00"), ledger())
    assert "2025-07-01 14:00:00" in error.reason


# ---------------------------------------------------------------------------
# TR-203 - naive timestamps are localised with the declared zone
# ---------------------------------------------------------------------------


def test_naive_localised() -> None:
    """A value with no offset is read in the source's declared zone. TR-203, D17."""
    config = {**STATEMENT_CONFIG, "timezone": "America/New_York"}
    fmt = source_format_from_config("statement", config)

    record = ok(replace(STATEMENT_ROW, executed_at="2025-07-01 09:15:00"), fmt)

    assert record.occurred_at == datetime(2025, 7, 1, 13, 15, tzinfo=UTC)
    assert record.occurred_at.tzinfo is not None
    assert record.occurred_at.utcoffset() == datetime(2025, 1, 1, tzinfo=UTC).utcoffset()


def test_declared_zone_is_applied_per_source_not_per_file() -> None:
    """Same wall clock, two sources, two instants. Guessing is what D17 forbids."""
    utc_fmt = source_format_from_config("statement", STATEMENT_CONFIG)
    berlin_fmt = source_format_from_config(
        "statement", {**STATEMENT_CONFIG, "timezone": "Europe/Berlin"}
    )
    row = replace(STATEMENT_ROW, executed_at="2025-07-01 09:15:00")

    assert ok(row, utc_fmt).occurred_at == datetime(2025, 7, 1, 9, 15, tzinfo=UTC)
    assert ok(row, berlin_fmt).occurred_at == datetime(2025, 7, 1, 7, 15, tzinfo=UTC)


def test_explicit_offset_beats_the_declared_zone() -> None:
    """A trailing Z is an offset the source stated; it is not overridden. TR-203."""
    fmt = source_format_from_config("ledger", {**LEDGER_CONFIG, "timezone": "America/New_York"})
    record = ok(LEDGER_ROW, fmt)
    assert record.occurred_at == datetime(2025, 7, 1, 14, 0, tzinfo=UTC)


def test_trailing_z_accepted_even_when_the_pattern_omits_it() -> None:
    """The same value read against a pattern that does not spell the Z out."""
    fmt = source_format_from_config(
        "ledger", {**LEDGER_CONFIG, "timestamp_formats": ["%Y-%m-%dT%H:%M:%S"]}
    )
    assert ok(LEDGER_ROW, fmt).occurred_at == datetime(2025, 7, 1, 14, 0, tzinfo=UTC)


def test_every_timestamp_is_aware_utc() -> None:
    """CLAUDE.md invariant 6, at the point the value is created."""
    for raw, fmt in (
        (LEDGER_ROW, ledger()),
        (STATEMENT_ROW, statement()),
        (VENUE_C_ROW, venue_c()),
    ):
        occurred_at = ok(raw, fmt).occurred_at
        assert occurred_at.tzinfo is not None
        assert occurred_at.utcoffset() == datetime(2025, 1, 1, tzinfo=UTC).utcoffset()


# ---------------------------------------------------------------------------
# TR-204 - coded values go through the declared vocabulary
# ---------------------------------------------------------------------------


def test_vocabulary() -> None:
    """BUY/B/d and SELL/S/c all arrive as one Side. TR-204, AC1."""
    assert ok(LEDGER_ROW, ledger()).side is Side.BUY
    assert ok(STATEMENT_ROW, statement()).side is Side.BUY
    assert ok(VENUE_C_ROW, venue_c()).side is Side.BUY

    assert ok(replace(LEDGER_ROW, side="SELL"), ledger()).side is Side.SELL
    assert ok(replace(STATEMENT_ROW, direction="S"), statement()).side is Side.SELL
    assert ok(replace(VENUE_C_ROW, bs="c"), venue_c()).side is Side.SELL

    assert ok(replace(VENUE_C_ROW, state="OK"), venue_c()).status is RecordStatus.SETTLED
    assert ok(replace(VENUE_C_ROW, state="VOID"), venue_c()).status is RecordStatus.CANCELLED


def test_unmapped_side_token_is_a_row_error_naming_the_token() -> None:
    """Never a silent pass-through and never a default: a wrong side is money. TR-204."""
    error = bad(replace(STATEMENT_ROW, direction="X"), statement())
    assert "'X'" in error.reason
    assert "side" in error.reason


def test_unmapped_status_token_is_a_row_error() -> None:
    error = bad(replace(LEDGER_ROW, state="PART_FILLED"), ledger())
    assert "PART_FILLED" in error.reason
    assert "status" in error.reason


def test_a_sides_token_is_not_read_as_a_status() -> None:
    """The two vocabularies are separate tables, not one shared lookup."""
    assert isinstance(bad(replace(LEDGER_ROW, state="BUY"), ledger()), RowError)


def test_cancelled_status_marks_the_record_excluded() -> None:
    """Feeds R3.1: exclusion reads the mapped status, not the source's word."""
    assert ok(replace(LEDGER_ROW, state="CANCELLED"), ledger()).is_cancelled
    assert ok(replace(VENUE_C_ROW, state="VOID"), venue_c()).is_cancelled
    assert not ok(LEDGER_ROW, ledger()).is_cancelled


# ---------------------------------------------------------------------------
# TR-205, TR-206 - exact decimals, at the precision received
# ---------------------------------------------------------------------------


def test_precision_preserved() -> None:
    """0.50 and 0.5 are the same number and different text. Both survive. TR-206."""
    padded = ok(replace(LEDGER_ROW, quantity="0.50"), ledger())
    trimmed = ok(replace(STATEMENT_ROW, qty="0.5"), statement())

    assert str(padded.quantity) == "0.50"
    assert str(trimmed.quantity) == "0.5"
    assert padded.quantity == trimmed.quantity
    assert padded.quantity.as_tuple().exponent == -2
    assert trimmed.quantity.as_tuple().exponent == -1

    assert str(ok(LEDGER_ROW, ledger()).quantity) == "8000.00"
    assert str(ok(STATEMENT_ROW, statement()).quantity) == "8000"


def test_no_rounding_at_load_time() -> None:
    """Rounding differences are settled by tolerance later, never by truncation. R2.5."""
    raw = replace(
        LEDGER_ROW,
        quantity="0.123456789012345678",
        price="61380.005",
        gross_amount="7577.53409876543210",
    )
    record = ok(raw, ledger())
    assert str(record.quantity) == "0.123456789012345678"
    assert str(record.unit_price) == "61380.005"
    assert str(record.gross_amount) == "7577.53409876543210"


def test_values_are_decimal_never_float() -> None:
    """TR-205 at runtime; test_no_float.py enforces it statically."""
    record = ok(LEDGER_ROW, ledger())
    for value in (record.quantity, record.unit_price, record.gross_amount):
        assert isinstance(value, Decimal)


def test_whitespace_and_thousands_separators_are_stripped() -> None:
    """Presentation, not precision: the digits are untouched. TR-206."""
    record = ok(
        replace(LEDGER_ROW, quantity=" 8,000.00 ", gross_amount="4 640.00"),
        ledger(),
    )
    assert str(record.quantity) == "8000.00"
    assert str(record.gross_amount) == "4640.00"


def test_negative_and_exponent_notation_survive() -> None:
    record = ok(replace(LEDGER_ROW, quantity="-1.50", price="5.8E-1"), ledger())
    assert record.quantity == Decimal("-1.50")
    assert record.unit_price == Decimal("0.58")


def test_unparseable_number_is_a_row_error() -> None:
    error = bad(replace(LEDGER_ROW, quantity="abc"), ledger())
    assert error.row_no == 2
    assert "quantity" in error.reason
    assert error.raw["quantity"] == "abc"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_numbers_are_rejected(value: str) -> None:
    """Decimal accepts these; a quantity is not one of them."""
    assert isinstance(bad(replace(LEDGER_ROW, gross_amount=value), ledger()), RowError)


# ---------------------------------------------------------------------------
# Missing and malformed rows
# ---------------------------------------------------------------------------


def test_missing_column_is_a_row_error() -> None:
    raw = {key: value for key, value in LEDGER_ROW.items() if key != "price"}
    error = bad(raw, ledger())
    assert "price" in error.reason
    assert "unit_price" in error.reason


def test_empty_value_is_a_row_error() -> None:
    error = bad(replace(LEDGER_ROW, trade_id="   "), ledger())
    assert "trade_id" in error.reason


def test_normalize_row_returns_errors_and_never_raises() -> None:
    """TR-106: a bad row is data. It cannot be allowed to stop the file."""
    for raw in ({}, {"trade_id": "T-1"}, dict.fromkeys(LEDGER_ROW, "")):
        assert isinstance(normalize_row(raw, ledger(), 7), RowError)


def test_normalize_rows_partitions_and_numbers_from_the_header() -> None:
    """Valid rows in the same file still load. TR-106, R1.5."""
    rows = [
        LEDGER_ROW,
        replace(LEDGER_ROW, trade_id="T-9998", traded_at="not-a-date"),
        replace(LEDGER_ROW, trade_id="T-1002"),
        replace(LEDGER_ROW, trade_id="T-9999", quantity="abc"),
    ]
    records, errors = normalize_rows(rows, ledger())

    assert [r.reference for r in records] == ["T-1001", "T-1002"]
    assert [e.row_no for e in errors] == [3, 5]
    assert [r.row_no for r in records] == [2, 4]
    assert errors[0].raw["trade_id"] == "T-9998"


def test_normalize_rows_row_numbers_can_be_rebased() -> None:
    records, _ = normalize_rows([LEDGER_ROW], ledger(), start_row_no=1)
    assert records[0].row_no == 1


# ---------------------------------------------------------------------------
# TR-101 support - the header is checked before any row is read
# ---------------------------------------------------------------------------


def test_validate_header_accepts_the_real_headers() -> None:
    validate_header(list(LEDGER_ROW), ledger())
    validate_header(list(STATEMENT_ROW), statement())
    validate_header(list(VENUE_C_ROW), venue_c())


def test_validate_header_rejects_a_missing_mapped_column() -> None:
    header = [name for name in LEDGER_ROW if name != "gross_amount"]
    with pytest.raises(HeaderError) as caught:
        validate_header(header, ledger())
    assert "gross_amount" in str(caught.value)


def test_validate_header_tolerates_extra_columns_and_stray_whitespace() -> None:
    """An extra column is the counterparty's business, not a reason to refuse."""
    header = [f" {name} " for name in LEDGER_ROW] + ["settlement_note"]
    validate_header(header, ledger())


def test_validate_header_ignores_a_byte_order_mark() -> None:
    header = ["﻿" + next(iter(LEDGER_ROW)), *list(LEDGER_ROW)[1:]]
    validate_header(header, ledger())


# ---------------------------------------------------------------------------
# TR-203 - configuration that cannot be trusted fails at load, not per row
# ---------------------------------------------------------------------------


def without(key: str) -> dict[str, Any]:
    return {k: v for k, v in LEDGER_CONFIG.items() if k != key}


def test_config_without_a_timezone_fails_validation() -> None:
    """TR-203: a source declaring no timezone is not loadable. D17."""
    with pytest.raises(FormatConfigError) as caught:
        source_format_from_config("ledger", without("timezone"))
    assert "timezone" in str(caught.value)


def test_config_with_an_unknown_timezone_fails_validation() -> None:
    with pytest.raises(FormatConfigError) as caught:
        source_format_from_config("ledger", {**LEDGER_CONFIG, "timezone": "Mars/Olympus_Mons"})
    assert "Mars/Olympus_Mons" in str(caught.value)


def test_config_with_no_timestamp_format_fails_validation() -> None:
    with pytest.raises(FormatConfigError):
        source_format_from_config("ledger", {**LEDGER_CONFIG, "timestamp_formats": []})
    with pytest.raises(FormatConfigError):
        source_format_from_config("ledger", without("timestamp_formats"))


@pytest.mark.parametrize("field", sorted(LEDGER_CONFIG["columns"]))
def test_config_missing_any_field_mapping_fails_validation(field: str) -> None:
    """All eight fields of the common set are required. TR-201, R2.1."""
    columns = {k: v for k, v in LEDGER_CONFIG["columns"].items() if k != field}
    with pytest.raises(FormatConfigError) as caught:
        source_format_from_config("ledger", {**LEDGER_CONFIG, "columns": columns})
    assert field in str(caught.value)


def test_config_with_an_unknown_field_name_fails_validation() -> None:
    """A typo is a configuration error, not a field that quietly never loads."""
    columns = {**LEDGER_CONFIG["columns"], "occured_at": "traded_at"}
    with pytest.raises(FormatConfigError) as caught:
        source_format_from_config("ledger", {**LEDGER_CONFIG, "columns": columns})
    assert "occured_at" in str(caught.value)


def test_config_with_an_unknown_vocabulary_target_fails_validation() -> None:
    """A side that is neither BUY nor SELL is caught once, not once per row."""
    with pytest.raises(FormatConfigError) as caught:
        source_format_from_config(
            "ledger", {**LEDGER_CONFIG, "side_map": {"BUY": "BUY", "SELL": "SHORT"}}
        )
    assert "SHORT" in str(caught.value)

    with pytest.raises(FormatConfigError) as caught:
        source_format_from_config("ledger", {**LEDGER_CONFIG, "status_map": {"SETTLED": "DONE"}})
    assert "DONE" in str(caught.value)


def test_config_rejects_an_empty_vocabulary() -> None:
    with pytest.raises(FormatConfigError):
        source_format_from_config("ledger", {**LEDGER_CONFIG, "side_map": {}})


def test_config_returns_the_declared_shape() -> None:
    fmt = venue_c()
    assert fmt.source_code == "venue_c"
    assert fmt.columns["reference"] == "txn_ref"
    assert tuple(fmt.timestamp_formats) == ("%s",)
    assert fmt.timezone == "UTC"
    assert fmt.side_map["d"] is Side.BUY
    assert fmt.status_map["VOID"] is RecordStatus.CANCELLED


# ---------------------------------------------------------------------------
# Configuration that is not merely wrong but malformed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("columns", "trade_id"),
        ("columns", {"reference": 7}),
        ("columns", {**LEDGER_CONFIG["columns"], "side": "  "}),
        ("timestamp_formats", "%Y-%m-%d"),
        ("timestamp_formats", [7]),
        ("timestamp_formats", ["  "]),
        ("timezone", 7),
        ("timezone", "   "),
        ("side_map", ["BUY"]),
        ("side_map", {7: "BUY"}),
        ("side_map", {"BUY": 7}),
        ("status_map", {"": "SETTLED"}),
    ],
)
def test_config_rejects_malformed_values(key: str, value: Any) -> None:
    """JSON from a database is checked, not assumed. DESIGN.md section 8."""
    with pytest.raises(FormatConfigError):
        source_format_from_config("ledger", {**LEDGER_CONFIG, key: value})


@pytest.mark.parametrize(
    "key", ["columns", "timestamp_formats", "timezone", "side_map", "status_map"]
)
def test_config_requires_every_key(key: str) -> None:
    """Nothing here has a default. A silent default is a guess. R2.2, TR-203."""
    with pytest.raises(FormatConfigError) as caught:
        source_format_from_config("ledger", without(key))
    assert key in str(caught.value)


def test_config_rejects_a_non_string_field_name() -> None:
    columns = {**LEDGER_CONFIG["columns"], 7: "trade_id"}
    with pytest.raises(FormatConfigError):
        source_format_from_config("ledger", {**LEDGER_CONFIG, "columns": columns})


def test_config_rejects_a_configuration_that_is_not_an_object() -> None:
    with pytest.raises(FormatConfigError):
        source_format_from_config("ledger", ["columns"])  # type: ignore[arg-type]


def test_config_requires_a_source_code() -> None:
    with pytest.raises(FormatConfigError):
        source_format_from_config("   ", LEDGER_CONFIG)


def test_config_tolerates_extra_top_level_keys() -> None:
    """The envelope may carry more than normalisation needs; it is not a defect."""
    fmt = source_format_from_config("ledger", {**LEDGER_CONFIG, "delivered_by": "sftp"})
    assert fmt.columns["reference"] == "trade_id"


# ---------------------------------------------------------------------------
# Defensive: a hand-built format, a short row, a hostile mapping
# ---------------------------------------------------------------------------


def handmade(**changes: Any) -> SourceFormat:
    """A SourceFormat assembled directly, bypassing configuration validation."""
    base: dict[str, Any] = {
        "source_code": "handmade",
        "columns": dict(LEDGER_CONFIG["columns"]),
        "timestamp_formats": ("%Y-%m-%dT%H:%M:%SZ",),
        "timezone": "UTC",
        "side_map": {"BUY": Side.BUY, "SELL": Side.SELL},
        "status_map": {"SETTLED": RecordStatus.SETTLED},
    }
    return SourceFormat(**{**base, **changes})


def test_a_field_with_no_mapped_column_is_a_row_error() -> None:
    columns = {k: v for k, v in LEDGER_CONFIG["columns"].items() if k != "instrument"}
    error = bad(LEDGER_ROW, handmade(columns=columns))
    assert "instrument" in error.reason


def test_an_undeclarable_timezone_is_a_row_error_not_a_crash() -> None:
    """Validation catches this at load; normalisation still refuses to raise."""
    fmt = handmade(timezone="Mars/Olympus_Mons", timestamp_formats=("%Y-%m-%dT%H:%M:%S",))
    error = bad(replace(LEDGER_ROW, traded_at="2025-07-01T14:00:00"), fmt)
    assert "Mars/Olympus_Mons" in error.reason


def test_a_short_row_missing_values_is_a_row_error() -> None:
    """csv.DictReader fills a short row with None rather than refusing it."""
    raw: dict[str, Any] = {**LEDGER_ROW, "gross_amount": None}
    error = bad(raw, ledger())
    assert "gross_amount" in error.reason


def test_a_trailing_z_that_still_does_not_parse_is_a_row_error() -> None:
    fmt = handmade(timestamp_formats=("%Y-%m-%dT%H:%M:%S",))
    assert isinstance(bad(replace(LEDGER_ROW, traded_at="not-a-dateZ"), fmt), RowError)


@pytest.mark.parametrize("epoch", ["NaN", "999999999999999999", "-999999999999999999"])
def test_an_impossible_epoch_is_a_row_error(epoch: str) -> None:
    assert isinstance(bad(replace(VENUE_C_ROW, ts_epoch=epoch), venue_c()), RowError)


class _HostileRow(dict[str, str]):
    """A mapping that fails on read. Stands in for anything unforeseen."""

    def __getitem__(self, key: str) -> str:
        raise RuntimeError("column store unavailable")


def test_an_unforeseen_failure_still_returns_a_row_error() -> None:
    """TR-106 is unconditional: whatever goes wrong, the file keeps loading."""
    result = normalize_row(_HostileRow(LEDGER_ROW), ledger(), 4)
    assert isinstance(result, RowError)
    assert result.row_no == 4
    assert "RuntimeError" in result.reason
    assert result.raw["trade_id"] == "T-1001"
