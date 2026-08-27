"""Configuration is validated before the first request, and means what it says.

Two different failures live here. One is a configuration that cannot load rows
at all, which must fail at startup rather than halfway through a morning run
(TR-705). The other is subtler and more dangerous: a configuration that loads
fine and is quietly wrong by a factor of ten thousand.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Source, ToleranceProfile
from app.seed import seed
from app.sources import DEFAULT_TOLERANCES, SOURCE_PAIRS, SOURCES
from core.compare import compare
from core.format import FormatConfigError, source_format_from_config
from core.model import NormalizedRecord, RecordStatus, Side, Verdict
from core.tolerance import ToleranceConfigError, tolerances_from_config


def record(
    reference: str = "T-1001",
    *,
    occurred_at: datetime | None = None,
    quantity: str = "10.00",
    unit_price: str = "3400.00",
    gross_amount: str = "34000.00",
) -> NormalizedRecord:
    return NormalizedRecord(
        source_code="ledger",
        reference=reference,
        occurred_at=occurred_at or datetime(2025, 7, 4, 10, 15, tzinfo=UTC),
        instrument="ETH-USD",
        side=Side.BUY,
        quantity=Decimal(quantity),
        unit_price=Decimal(unit_price),
        gross_amount=Decimal(gross_amount),
        status=RecordStatus.SETTLED,
        row_no=2,
        raw={},
    )


# ---------------------------------------------------------------------------
# The shipped configuration is correct, in the units it claims
# ---------------------------------------------------------------------------


def test_shipped_tolerances_classify_the_brief_s_own_example() -> None:
    """The regression that motivated this test.

    Basis points are fractions in ``core.model.Tolerances``: five basis points
    is ``0.0005``. Written as ``5`` the allowance becomes 500%, every threshold
    silently passes, and the reconciliation reports a clean morning every day.
    Nothing else in the suite would have caught it, because a comparison test
    that builds its own ``Tolerances`` never reads the shipped configuration.

    The numbers here are the brief's, not ours.
    """
    tol = tolerances_from_config(DEFAULT_TOLERANCES)

    # 34,000.00 against 34,170.00 is 0.4975%, ten times the five-bps threshold.
    result = compare(record(), record(gross_amount="34170.00", unit_price="3417.00"), tol)
    assert result.verdict is Verdict.BREAK

    # 10:00 against 10:40 is forty minutes against a five-minute tolerance.
    late = record(occurred_at=datetime(2025, 7, 5, 10, 40, tzinfo=UTC))
    assert (
        compare(record(occurred_at=datetime(2025, 7, 5, 10, 0, tzinfo=UTC)), late, tol).verdict
        is Verdict.BREAK
    )

    # 0.50 against 0.5 is formatting, not a difference.
    assert compare(record(quantity="0.50"), record(quantity="0.5"), tol).verdict is Verdict.AGREED

    # A couple of cents on a 34,000 trade is fee drift, and must not reach a person.
    drift = compare(record(), record(gross_amount="34000.02"), tol)
    assert drift.verdict is Verdict.AGREED_WITH_DRIFT


def test_basis_point_thresholds_are_fractions_not_counts() -> None:
    """Guards the units directly, so the failure names itself."""
    tol = tolerances_from_config(DEFAULT_TOLERANCES)
    for name, value in (
        ("amount_bps", tol.amount_bps),
        ("price_bps", tol.price_bps),
        ("qty_bps", tol.qty_bps),
    ):
        assert value < Decimal("0.01"), (
            f"{name} is {value}; basis points are fractions, so 5 bps is 0.0005. "
            "A whole number here makes the allowance thousands of percent."
        )


def test_every_shipped_source_config_validates() -> None:
    """TR-203. A config that cannot load rows must not reach the database."""
    for code, config in SOURCES.items():
        fmt = source_format_from_config(code, config)
        assert fmt.source_code == code
        assert fmt.timezone
        assert fmt.timestamp_formats


def test_shipped_tolerances_parse() -> None:
    assert tolerances_from_config(DEFAULT_TOLERANCES).time_tolerance_seconds == 300


# ---------------------------------------------------------------------------
# Bad configuration fails fast
# ---------------------------------------------------------------------------


def test_fails_fast() -> None:
    """TR-705. Invalid configuration raises at load time, not mid-run."""
    without_timezone = {k: v for k, v in SOURCES["statement"].items() if k != "timezone"}
    with pytest.raises(FormatConfigError):
        source_format_from_config("statement", without_timezone)

    missing_threshold = {k: v for k, v in DEFAULT_TOLERANCES.items() if k != "amount_bps"}
    with pytest.raises(ToleranceConfigError):
        tolerances_from_config(missing_threshold)


def test_unknown_timezone_is_rejected() -> None:
    with pytest.raises(FormatConfigError):
        source_format_from_config("statement", {**SOURCES["statement"], "timezone": "Mars/Olympus"})


def test_missing_column_mapping_is_rejected() -> None:
    columns = {k: v for k, v in SOURCES["ledger"]["columns"].items() if k != "gross_amount"}
    with pytest.raises(FormatConfigError):
        source_format_from_config("ledger", {**SOURCES["ledger"], "columns": columns})


def test_unparseable_threshold_is_rejected() -> None:
    with pytest.raises(ToleranceConfigError):
        tolerances_from_config({**DEFAULT_TOLERANCES, "amount_bps": "five"})


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def test_seed_creates_sources_and_profiles(db_session: Session) -> None:
    seed(db_session)
    codes = set(db_session.scalars(select(Source.code)))
    assert codes == set(SOURCES)
    assert len(list(db_session.scalars(select(ToleranceProfile)))) == len(SOURCE_PAIRS)


def test_seed_is_idempotent(db_session: Session) -> None:
    """`make seed` runs on every setup; a second run must not duplicate."""
    seed(db_session)
    seed(db_session)
    assert len(list(db_session.scalars(select(Source)))) == len(SOURCES)
    assert len(list(db_session.scalars(select(ToleranceProfile)))) == len(SOURCE_PAIRS)


def test_seeded_profile_survives_a_round_trip(db_session: Session) -> None:
    """The thresholds a run actually reads are the ones the config states."""
    seed(db_session)
    profile = db_session.scalars(select(ToleranceProfile)).first()
    assert profile is not None
    assert profile.amount_bps == Decimal(DEFAULT_TOLERANCES["amount_bps"])
    assert profile.amount_abs_floor == Decimal(DEFAULT_TOLERANCES["amount_abs_floor"])
    assert profile.time_tolerance_seconds == 300
