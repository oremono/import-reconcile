"""Generate the sample CSVs.

Committed so a reviewer can see how the cases were built rather than taking the
data on trust. Deterministic: running it twice produces byte-identical files,
which matters because one of those files is the duplicate-detection fixture.

Every case named in DESIGN.md section 10 is seeded here, and each row carries a
``case`` tag in this file so the intent of each is legible.
"""

from __future__ import annotations

import csv
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"
PERIOD = "2025-07-01_07"
START = datetime(2025, 7, 1, 9, 0, tzinfo=UTC)

INSTRUMENTS = {
    "BTC-USD": Decimal("62000"),
    "ETH-USD": Decimal("3400"),
    "SOL-USD": Decimal("146"),
    "ADA-USD": Decimal("0.58"),
}

# case -> what the counterparty's version does differently
CASES = [
    # (case, instrument, side, qty, price, ledger_status)
    ("verbatim_agree", "BTC-USD", "BUY", "0.50", "62000.00", "SETTLED"),
    ("verbatim_amount_break", "ETH-USD", "BUY", "10.00", "3400.00", "SETTLED"),
    ("verbatim_time_break", "SOL-USD", "SELL", "300.00", "146.00", "SETTLED"),
    ("verbatim_ledger_only", "BTC-USD", "BUY", "0.20", "63200.00", "SETTLED"),
    ("verbatim_cancelled", "SOL-USD", "BUY", "100.00", "149.00", "CANCELLED"),
    ("drift_amount", "BTC-USD", "SELL", "0.75", "61800.00", "SETTLED"),
    ("drift_time", "ETH-USD", "SELL", "5.00", "3380.00", "SETTLED"),
    ("break_price_and_amount", "BTC-USD", "BUY", "1.25", "62500.00", "SETTLED"),
    ("break_side", "ADA-USD", "BUY", "10000.00", "0.58", "SETTLED"),
    ("break_status", "ETH-USD", "BUY", "2.00", "3410.00", "SETTLED"),
    ("cancelled_both", "ADA-USD", "SELL", "5000.00", "0.59", "CANCELLED"),
    ("cancelled_ledger_only", "SOL-USD", "BUY", "50.00", "147.00", "CANCELLED"),
    ("ledger_only", "ETH-USD", "SELL", "3.00", "3395.00", "SETTLED"),
    ("suggestion", "BTC-USD", "SELL", "0.40", "62100.00", "SETTLED"),
    ("drift_amount", "SOL-USD", "BUY", "200.00", "148.00", "SETTLED"),
    ("break_amount", "ADA-USD", "BUY", "20000.00", "0.57", "SETTLED"),
]


def _gross(qty: str, price: str) -> str:
    return str((Decimal(qty) * Decimal(price)).quantize(Decimal("0.01")))


def _trim(value: str) -> str:
    """Drop trailing zeros the way a counterparty's system would, in plain notation.

    ``Decimal.normalize`` alone yields "8E+3", which no real statement writes.
    """
    return format(Decimal(value).normalize(), "f")


def _bump(amount: str, delta: str) -> str:
    return str((Decimal(amount) + Decimal(delta)).quantize(Decimal("0.01")))


def build() -> tuple[list[dict], list[dict], list[dict]]:
    """Return (ledger rows, statement rows, venue_c rows) as dicts."""
    rng = random.Random(20250701)
    ledger: list[dict] = []
    statement: list[dict] = []
    venue_c: list[dict] = []

    at = START
    ref_no = 1001

    def next_slot() -> datetime:
        nonlocal at
        at = at + timedelta(hours=rng.choice([1, 2, 3, 5, 7]))
        return at

    # Filler agreeing trades, so the interesting cases sit in a realistic file.
    plan: list[tuple[str, str, str, str, str, str]] = []
    for _ in range(24):
        instrument = rng.choice(list(INSTRUMENTS))
        base = INSTRUMENTS[instrument]
        price = (base * Decimal(rng.randint(980, 1020)) / Decimal(1000)).quantize(Decimal("0.01"))
        qty = {"BTC-USD": "0.30", "ETH-USD": "4.00", "SOL-USD": "150.00", "ADA-USD": "8000.00"}[
            instrument
        ]
        plan.append(("agree", instrument, rng.choice(["BUY", "SELL"]), qty, str(price), "SETTLED"))
    plan[8:8] = CASES  # interleave rather than clumping the interesting rows

    for case, instrument, side, qty, price, status in plan:
        ts = next_slot()
        ref = f"T-{ref_no}"
        ref_no += 1
        gross = _gross(qty, price)

        lrow = {
            "trade_id": ref,
            "traded_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "instrument": instrument,
            "side": side,
            "quantity": qty,
            "price": price,
            "gross_amount": gross,
            "state": status,
            "_case": case,
        }
        ledger.append(lrow)

        if case in {"ledger_only", "verbatim_ledger_only"}:
            continue

        srow = {
            "reference": ref,
            "executed_at": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": instrument,
            "direction": "B" if side == "BUY" else "S",
            "qty": _trim(qty),
            "unit_price": _trim(price),
            "total": gross,
            "status": status,
            "_case": case,
        }

        # A sub-dollar instrument needs more than two decimal places for a
        # half-percent price move to survive rounding. Quantising a broken
        # price to cents silently un-breaks it on ADA at 0.57, which is how a
        # case that claims to be a break ends up agreeing.
        price_step = Decimal("0.00000001") if Decimal(price) < 1 else Decimal("0.01")

        if case == "drift_amount":
            srow["total"] = _bump(gross, "0.02")
        elif case == "drift_time":
            srow["executed_at"] = (ts + timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S")
        elif case in {"verbatim_amount_break", "break_amount"}:
            new_price = (Decimal(price) * Decimal("1.005")).quantize(price_step)
            srow["unit_price"] = _trim(str(new_price))
            srow["total"] = _gross(qty, str(new_price))
        elif case == "break_price_and_amount":
            new_price = (Decimal(price) + Decimal("450")).quantize(price_step)
            srow["unit_price"] = _trim(str(new_price))
            srow["total"] = _gross(qty, str(new_price))
        elif case == "verbatim_time_break":
            srow["executed_at"] = (ts + timedelta(minutes=40)).strftime("%Y-%m-%d %H:%M:%S")
        elif case == "break_side":
            srow["direction"] = "S" if side == "BUY" else "B"
        elif case == "break_status":
            srow["status"] = "PENDING"
        elif case == "cancelled_ledger_only":
            srow["status"] = "SETTLED"
        elif case == "suggestion":
            srow["reference"] = f"C-9{ref_no:03d}"  # same trade, different identifier
            srow["executed_at"] = (ts + timedelta(minutes=12)).strftime("%Y-%m-%d %H:%M:%S")

        statement.append(srow)

    # Statement-only rows: things the counterparty says happened and we do not.
    for n, (instrument, side, qty, price) in enumerate(
        [("BTC-USD", "B", "0.15", "63100.00"), ("ETH-USD", "S", "1.50", "3402.00")]
    ):
        ts = next_slot()
        statement.append(
            {
                "reference": f"C-900{n + 1}",
                "executed_at": ts.strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": instrument,
                "direction": side,
                "qty": qty,
                "unit_price": price,
                "total": _gross(qty, price),
                "status": "SETTLED",
                "_case": "statement_only",
            }
        )

    # A third counterparty, third format: epoch seconds, d/c side codes, own columns.
    for lrow in ledger[:12]:
        ts = datetime.strptime(lrow["traded_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        venue_c.append(
            {
                "txn_ref": lrow["trade_id"],
                "ts_epoch": str(int(ts.timestamp())),
                "pair": lrow["instrument"],
                "bs": "d" if lrow["side"] == "BUY" else "c",
                "volume": lrow["quantity"],
                "rate": lrow["price"],
                "value": lrow["gross_amount"],
                "state": "OK" if lrow["state"] == "SETTLED" else "VOID",
                "_case": lrow["_case"],
            }
        )

    return ledger, statement, venue_c


def write(path: Path, rows: list[dict], extra_bad: list[str] | None = None) -> None:
    fields = [k for k in rows[0] if not k.startswith("_")]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        for line in extra_bad or []:
            fh.write(line + "\n")
    print(
        f"{path.name}: {len(rows)} rows" + (f" + {len(extra_bad)} malformed" if extra_bad else "")
    )


def main() -> None:
    DATA.mkdir(exist_ok=True)
    ledger, statement, venue_c = build()

    # Two rows that cannot be normalised: an unparseable date and a bad number.
    bad = [
        "T-9998,not-a-date,BTC-USD,BUY,1.00,62000.00,62000.00,SETTLED",
        "T-9999,2025-07-06T11:00:00Z,ETH-USD,BUY,abc,3400.00,3400.00,SETTLED",
    ]

    write(DATA / f"ledger_{PERIOD}.csv", ledger, bad)
    write(DATA / f"statement_{PERIOD}.csv", statement)

    # A byte-identical resend. Duplicate-detection fixture.
    resend = DATA / f"statement_{PERIOD}_resend.csv"
    resend.write_bytes((DATA / f"statement_{PERIOD}.csv").read_bytes())
    print(f"{resend.name}: byte-identical copy")

    # The correction: three amounts fixed, one row withdrawn.
    corrected = [dict(r) for r in statement]
    fixed = 0
    for row in corrected:
        if row["_case"] in {"verbatim_amount_break", "break_amount", "break_price_and_amount"}:
            ledger_row = next(r for r in ledger if r["trade_id"] == row["reference"])
            row["unit_price"] = _trim(ledger_row["price"])
            row["total"] = ledger_row["gross_amount"]
            fixed += 1
    withdrawn = corrected.pop()
    write(DATA / f"statement_{PERIOD}_v2.csv", corrected)
    print(f"  correction: {fixed} amounts fixed, {withdrawn['reference']} withdrawn")

    write(DATA / f"venue_c_{PERIOD}.csv", venue_c)


if __name__ == "__main__":
    main()
