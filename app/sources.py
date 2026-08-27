"""Source configuration and tolerance defaults. Plain data, no logic.

This module is the single authority for "what does each counterparty's file
look like" and "how much drift is benign". Tests and the seed both read it, so
the two cannot drift apart.

Adding a third counterparty means adding a dict here and a row in the database.
No code in ``core/`` or ``app/services/`` names a source, and
``tests/integration/test_third_source.py`` asserts that structurally rather
than trusting it (R2.4, TR-207, AC12).
"""

from __future__ import annotations

from typing import Any

from core.format import EPOCH_SECONDS

# ---------------------------------------------------------------------------
# Source formats
# ---------------------------------------------------------------------------

#: Our own ledger. ISO-8601 with an explicit Z, full words for side and status.
LEDGER: dict[str, Any] = {
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

#: The counterparty statement. Different column names, a space instead of a T,
#: no timezone at all, and single-letter side codes.
STATEMENT: dict[str, Any] = {
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

#: A third counterparty, added as configuration alone: epoch-second timestamps,
#: debit/credit side codes, and its own words for status.
VENUE_C: dict[str, Any] = {
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
    "timestamp_formats": [EPOCH_SECONDS],
    "timezone": "UTC",
    "side_map": {"d": "BUY", "c": "SELL"},
    "status_map": {"OK": "SETTLED", "VOID": "CANCELLED"},
}

SOURCES: dict[str, dict[str, Any]] = {
    "ledger": LEDGER,
    "statement": STATEMENT,
    "venue_c": VENUE_C,
}

SOURCE_NAMES: dict[str, str] = {
    "ledger": "Our trade ledger",
    "statement": "Counterparty statement",
    "venue_c": "Venue C statement",
}

# ---------------------------------------------------------------------------
# Tolerances
# ---------------------------------------------------------------------------

#: SPEC section 5.5, stated once.
#:
#: Basis-point thresholds are **fractions**, matching ``core.model.Tolerances``:
#: five basis points is ``"0.0005"``, not ``"5"``. The distinction is not
#: cosmetic. Under the wrong reading the allowance becomes 500%, and the
#: worked example from the brief - 34,000.00 against 34,170.00 - would report
#: as agreed rather than as the break it is.
#: ``tests/integration/test_config.py`` pins that with the brief's own numbers.
DEFAULT_TOLERANCES: dict[str, str] = {
    "amount_bps": "0.0005",  # 5 bps
    "amount_abs_floor": "0.01",
    "price_bps": "0.0005",  # 5 bps
    "qty_bps": "0.0001",  # 1 bp
    "time_tolerance_seconds": "300",  # 5 minutes
    "suggest_window_seconds": "7200",  # 2 hours, D5
}

#: Which source pairs get reconciled, and with which profile.
SOURCE_PAIRS: tuple[tuple[str, str], ...] = (
    ("ledger", "statement"),
    ("ledger", "venue_c"),
)
