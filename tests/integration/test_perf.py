"""TR-701. Ten thousand records a side, in under ten seconds, on SQLite.

The budget is not about speed for its own sake. A reconciliation an analyst
runs at 8am and reads at 8:01 is a tool; one that takes a coffee break is a
batch job, and people stop re-running it after a correction - which is exactly
when re-running matters most.

The data is generated here rather than committed, because a 20,000-row fixture
in the repository is a liability that nobody reads and everybody has to clone.
"""

from __future__ import annotations

import io
import time
from csv import DictWriter
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Pair, Record, RunItem
from app.services.ingest import ingest_file
from app.services.reconcile import run_reconciliation, run_summary, worklist

PERIOD = (date(2025, 7, 1), date(2025, 7, 7))
SCALE = 10_000
BUDGET_SECONDS = 10

INSTRUMENTS = ("BTC-USD", "ETH-USD", "ADA-USD", "SOL-USD")
START = datetime(2025, 7, 1, tzinfo=UTC)

#: Every hundredth pair is given an amount the statement disagrees with, so the
#: measurement covers the expensive path - comparing, and writing field diffs -
#: rather than only the happy one.
BREAK_EVERY = 100


def _rows(scale: int) -> list[dict[str, str]]:
    """One side's worth of plausible trades. Deterministic; no random seed to forget."""
    rows = []
    for n in range(scale):
        quantity = Decimal(1 + n % 50)
        price = Decimal("100.00") + Decimal(n % 977) / Decimal(100)
        rows.append(
            {
                "reference": f"P-{n:06d}",
                "occurred_at": START + timedelta(seconds=n * 30),
                "instrument": INSTRUMENTS[n % len(INSTRUMENTS)],
                "side": "BUY" if n % 2 == 0 else "SELL",
                "quantity": quantity,
                "unit_price": price,
                "gross_amount": (quantity * price).quantize(Decimal("0.01")),
            }
        )
    return rows


def _ledger_csv(rows) -> bytes:
    buffer = io.StringIO()
    writer = DictWriter(
        buffer,
        fieldnames=[
            "trade_id",
            "traded_at",
            "instrument",
            "side",
            "quantity",
            "price",
            "gross_amount",
            "state",
        ],
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "trade_id": row["reference"],
                "traded_at": row["occurred_at"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                "instrument": row["instrument"],
                "side": row["side"],
                "quantity": format(row["quantity"], "f"),
                "price": format(row["unit_price"], "f"),
                "gross_amount": format(row["gross_amount"], "f"),
                "state": "SETTLED",
            }
        )
    return buffer.getvalue().encode()


def _statement_csv(rows) -> bytes:
    buffer = io.StringIO()
    writer = DictWriter(
        buffer,
        fieldnames=[
            "reference",
            "executed_at",
            "symbol",
            "direction",
            "qty",
            "unit_price",
            "total",
            "status",
        ],
    )
    writer.writeheader()
    for index, row in enumerate(rows):
        amount = row["gross_amount"]
        if index % BREAK_EVERY == 0:
            amount = (amount + Decimal("25.00")).quantize(Decimal("0.01"))
        writer.writerow(
            {
                "reference": row["reference"],
                "executed_at": row["occurred_at"].strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": row["instrument"],
                "direction": "B" if row["side"] == "BUY" else "S",
                "qty": format(row["quantity"], "f"),
                "unit_price": format(row["unit_price"], "f"),
                "total": format(amount, "f"),
                "status": "SETTLED",
            }
        )
    return buffer.getvalue().encode()


def test_run_duration(db_session: Session, seeded_sources) -> None:
    """TR-701. The run itself, timed. Loading the files is not part of the budget.

    Ingest is measured by its own requirements; what this pins is the thing an
    analyst waits on after pressing the button.
    """
    rows = _rows(SCALE)
    ingest_file(db_session, seeded_sources.ledger, _ledger_csv(rows), *PERIOD, "big-ledger.csv")
    ingest_file(
        db_session, seeded_sources.statement, _statement_csv(rows), *PERIOD, "big-statement.csv"
    )
    db_session.flush()
    assert db_session.scalar(select(func.count()).select_from(Record)) == SCALE * 2

    started = time.monotonic()
    run = run_reconciliation(db_session, seeded_sources.ledger, seeded_sources.statement, *PERIOD)
    db_session.flush()
    elapsed = time.monotonic() - started

    counts = run_summary(db_session, run.id)
    assert sum(counts.values()) == SCALE * 2, "the budget is meaningless if the run lost rows"
    assert counts["break"] == 2 * (SCALE // BREAK_EVERY)
    assert (
        db_session.scalar(select(func.count()).select_from(Pair).where(Pair.run_id == run.id))
        == SCALE
    )
    assert (
        db_session.scalar(select(func.count()).select_from(RunItem).where(RunItem.run_id == run.id))
        == SCALE * 2
    )

    # The page an analyst opens next has to be quick too, or the budget above
    # just moves the wait one click later.
    listed = time.monotonic()
    assert len(worklist(db_session, run.id)) == counts["break"]
    worklist_seconds = time.monotonic() - listed

    assert elapsed < BUDGET_SECONDS, (
        f"a run over {SCALE} records a side took {elapsed:.2f}s, budget {BUDGET_SECONDS}s"
    )
    assert worklist_seconds < BUDGET_SECONDS
