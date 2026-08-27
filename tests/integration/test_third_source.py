"""A third counterparty is configuration, not code.

R2.4 is the requirement the whole normalisation design exists to satisfy, and
AC12 is how a reviewer checks it. Asserting it in prose would be worthless, so
this proves it two ways: venue C's file loads correctly through the same path
the other two use, and no module in ``core/`` or ``app/services/`` contains the
string that would betray a special case.
"""

from __future__ import annotations

import ast
import csv
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Source
from app.seed import seed
from app.sources import SOURCES
from core.format import source_format_from_config
from core.model import RecordStatus, Side
from core.normalize import normalize_rows

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
VENUE_C_FILE = DATA / "venue_c_2025-07-01_07.csv"
LEDGER_FILE = DATA / "ledger_2025-07-01_07.csv"
STATEMENT_FILE = DATA / "statement_2025-07-01_07.csv"


def load(path: Path, code: str):
    fmt = source_format_from_config(code, SOURCES[code])
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return normalize_rows(rows, fmt)


# ---------------------------------------------------------------------------
# It loads, driven only by configuration
# ---------------------------------------------------------------------------


def test_third_source_loads_from_configuration_alone() -> None:
    """TR-207, AC12. Twelve rows, no errors, no code that knows what venue C is."""
    records, errors = load(VENUE_C_FILE, "venue_c")
    assert errors == ()
    assert len(records) == 12


def test_epoch_seconds_resolve_to_the_same_instant_as_the_other_two_sources() -> None:
    """The sharpest version of the claim.

    Three sources, three timestamp formats - ISO with a Z, a space-separated
    local time, and epoch seconds - and one instant. If the configuration is
    doing the work, T-1001 lands on the same moment from all three.
    """
    venue, _ = load(VENUE_C_FILE, "venue_c")
    ledger, _ = load(LEDGER_FILE, "ledger")
    statement, _ = load(STATEMENT_FILE, "statement")

    def instant(records, reference: str) -> datetime:
        return next(r.occurred_at for r in records if r.reference == reference)

    assert instant(venue, "T-1001") == instant(ledger, "T-1001") == instant(statement, "T-1001")
    assert instant(venue, "T-1001").tzinfo is not None


def test_debit_credit_side_codes_map_correctly() -> None:
    """`d`/`c` is a third vocabulary for the same two directions."""
    venue, _ = load(VENUE_C_FILE, "venue_c")
    ledger, _ = load(LEDGER_FILE, "ledger")
    by_reference = {r.reference: r.side for r in ledger}
    for rec in venue:
        assert rec.side in (Side.BUY, Side.SELL)
        assert rec.side == by_reference[rec.reference]


def test_third_source_status_vocabulary_maps_to_ours() -> None:
    """`OK`/`VOID` rather than `SETTLED`/`CANCELLED`, and cancelled still reads as cancelled."""
    venue, _ = load(VENUE_C_FILE, "venue_c")
    assert {r.status for r in venue} <= {RecordStatus.SETTLED, RecordStatus.CANCELLED}
    ledger, _ = load(LEDGER_FILE, "ledger")
    cancelled_in_ledger = {r.reference for r in ledger if r.is_cancelled}
    cancelled_in_venue = {r.reference for r in venue if r.is_cancelled}
    assert cancelled_in_venue == cancelled_in_ledger & {r.reference for r in venue}


def test_amounts_are_exact_across_a_third_format() -> None:
    venue, _ = load(VENUE_C_FILE, "venue_c")
    first = next(r for r in venue if r.reference == "T-1001")
    assert first.gross_amount == Decimal("4640.00")
    assert first.quantity * first.unit_price == first.gross_amount


# ---------------------------------------------------------------------------
# And no code knows it exists
# ---------------------------------------------------------------------------


CODE_PACKAGES = (ROOT / "core", ROOT / "app" / "services")


def _source_files() -> list[Path]:
    out: list[Path] = []
    for package in CODE_PACKAGES:
        if package.exists():
            out += [p for p in package.rglob("*.py") if "__pycache__" not in p.parts]
    return sorted(out)


def _code_literals(path: Path) -> set[str]:
    """String literals a module actually evaluates, excluding docstrings.

    Prose is not a special case. "double-counting a statement is worse than any
    error message" in a docstring says nothing about behaviour, whereas
    ``source.code == "statement"`` says everything - so only literals the
    interpreter reaches are examined.
    """
    tree = ast.parse(path.read_text())
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    }


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_no_module_names_a_source(name: str) -> None:
    """The claim is that adding a source touches no code, so test it.

    ``app/sources.py`` is the one place a source may be named, and it is data.
    """
    offenders = [str(p.relative_to(ROOT)) for p in _source_files() if name in _code_literals(p)]
    assert not offenders, f"{name!r} appears in code that should not know it exists: {offenders}"


def test_normalisation_has_no_conditional_on_source_code() -> None:
    """A branch on ``source_code`` would be a special case wearing a disguise."""
    offenders = []
    for path in _source_files():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.If) and "source_code" in ast.dump(node.test):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, f"code branches on which source it is handling: {offenders}"


def test_adding_the_third_source_is_one_database_row(db_session: Session) -> None:
    """End to end: seeding installs venue C with no migration and no code change."""
    seed(db_session)
    venue = db_session.scalar(select(Source).where(Source.code == "venue_c"))
    assert venue is not None
    fmt = source_format_from_config(venue.code, venue.format_config)
    assert fmt.side_map["d"] is Side.BUY
