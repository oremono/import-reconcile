"""The storage boundary, proved rather than asserted in a docstring.

Two column types and five constraints carry requirements that no application
code can be trusted with, because application code can be bypassed by the next
endpoint somebody writes. Each test below provokes the failure and checks that
the database, or the type, refuses it.

| Test | Requirement |
|---|---|
| ``test_decimal_roundtrip``            | TR-505 |
| ``test_utc_only``                     | TR-506 |
| ``test_precision_beyond_scale_raises``| TR-206 |
| ``test_duplicate_batch_hash_refused`` | TR-103, TR-503 |
| ``test_pair_uniqueness``              | TR-311, TR-504 |
| ``test_run_item_uniqueness``          | TR-509 |
| ``test_alembic_upgrade_head``         | TR-507 |
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, StaticPool, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.db.models import Base, FileBatch, Pair, Record, Run, RunItem
from app.db.types import ExactDecimal
from tests.integration.conftest import SeededSources

ROOT = Path(__file__).resolve().parents[2]

AWARE = datetime(2025, 7, 1, 14, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Builders. Foreign keys are enforced, so a row needs its parents.
# ---------------------------------------------------------------------------


def make_batch(
    session: Session,
    source_id: int,
    content_hash: str,
    filename: str = "ledger_2025-07-01_07.csv",
) -> FileBatch:
    batch = FileBatch(
        source_id=source_id,
        period_start=date(2025, 7, 1),
        period_end=date(2025, 7, 7),
        filename=filename,
        content_hash=content_hash,
    )
    session.add(batch)
    session.flush()
    return batch


def make_record(
    session: Session,
    batch: FileBatch,
    reference: str,
    *,
    quantity: object = Decimal("0.50"),
    unit_price: object = Decimal("62000.00"),
    gross_amount: object = Decimal("31000.00"),
    occurred_at: object = AWARE,
    row_no: int = 1,
) -> Record:
    record = Record(
        batch_id=batch.id,
        source_id=batch.source_id,
        reference=reference,
        occurred_at=occurred_at,
        instrument="BTC-USD",
        side="BUY",
        quantity=quantity,
        unit_price=unit_price,
        gross_amount=gross_amount,
        status="SETTLED",
        row_no=row_no,
        raw={},
    )
    session.add(record)
    session.flush()
    return record


def make_run(session: Session, sources: SeededSources) -> Run:
    run = Run(
        left_source_id=sources.ledger.id,
        right_source_id=sources.statement.id,
        period_start=date(2025, 7, 1),
        period_end=date(2025, 7, 7),
        counts={},
    )
    session.add(run)
    session.flush()
    return run


# ---------------------------------------------------------------------------
# TR-505 -- exact decimals, no float anywhere on the path
# ---------------------------------------------------------------------------

# Chosen for what each one would break. "0.50" against "0.5" is the two sources
# writing the same quantity differently and must compare equal. 0.00000001 is a
# satoshi, the smallest thing anyone books. The large value and the negative are
# the ends of the range; the negative also exercises the sign prefix that keeps
# the stored text sortable.
ROUNDTRIP_VALUES = [
    Decimal("0.50"),
    Decimal("0.5"),
    Decimal("31000.00"),
    Decimal("0.00000001"),
    Decimal("98765432109876.12345"),
    Decimal("-4321.99"),
]


def test_decimal_roundtrip(db_session: Session, seeded_sources: SeededSources) -> None:
    """TR-505. Every money value comes back the Decimal that went in.

    Not "close to". SQLAlchemy's ``Numeric`` degrades to float on SQLite, which
    would make ``0.1 + 0.2`` a reconciliation break. ``ExactDecimal`` stores
    text, so the assertion can be exact equality on both backends.
    """
    batch = make_batch(db_session, seeded_sources.ledger.id, content_hash="a" * 64)

    written = {}
    for n, value in enumerate(ROUNDTRIP_VALUES):
        record = make_record(
            db_session,
            batch,
            reference=f"T-{n}",
            quantity=value,
            unit_price=value,
            gross_amount=value,
            row_no=n,
        )
        written[record.id] = value

    db_session.commit()
    db_session.expire_all()

    for record_id, value in written.items():
        loaded = db_session.get(Record, record_id)
        assert loaded is not None
        for field in ("quantity", "unit_price", "gross_amount"):
            got = getattr(loaded, field)
            assert isinstance(got, Decimal), f"{field} came back a {type(got).__name__}"
            assert got == value, f"{field}: {got} != {value}"

    # 0.50 and 0.5 are the same quantity written two ways, and must compare so.
    assert Decimal("0.50") == Decimal("0.5")


def test_decimal_columns_are_text(engine: Engine) -> None:
    """TR-505. The storage is text, so no float exists to lose precision in."""
    columns = {c["name"]: c for c in inspect(engine).get_columns("record")}
    for field in ("quantity", "unit_price", "gross_amount"):
        rendered = str(columns[field]["type"]).upper()
        assert "CHAR" in rendered or "TEXT" in rendered, f"{field} is stored as {rendered}"


def test_stored_value_is_a_string_with_every_digit(
    db_session: Session, seeded_sources: SeededSources
) -> None:
    """TR-505. Read past the ORM: what is actually on disk is text, not a number."""
    batch = make_batch(db_session, seeded_sources.ledger.id, content_hash="b" * 64)
    record = make_record(db_session, batch, "T-RAW", quantity=Decimal("0.00000001"))
    db_session.commit()

    raw = db_session.execute(
        text("SELECT quantity FROM record WHERE id = :id"), {"id": record.id}
    ).scalar_one()
    assert isinstance(raw, str), f"stored as {type(raw).__name__}, so it went through a number"
    assert raw.endswith("0.000000010000"), raw
    assert Decimal(raw.lstrip("-0") or "0") == Decimal("0.00000001")


def test_float_is_refused_not_converted(db_session: Session, seeded_sources: SeededSources) -> None:
    """TR-505, CLAUDE.md invariant 1. A float on the money path raises."""
    batch = make_batch(db_session, seeded_sources.ledger.id, content_hash="c" * 64)
    with pytest.raises(StatementError) as caught:
        make_record(db_session, batch, "T-FLOAT", quantity=0.1 + 0.2)
    assert isinstance(caught.value.orig, TypeError)
    assert "float" in str(caught.value.orig)


def test_precision_beyond_scale_raises(db_session: Session, seeded_sources: SeededSources) -> None:
    """TR-206. Values are stored at the precision received, or not at all.

    Silently rounding the thirteenth decimal place is how a system manufactures
    a break it then asks a person to investigate.
    """
    batch = make_batch(db_session, seeded_sources.ledger.id, content_hash="d" * 64)
    too_precise = Decimal("0.1234567890123")  # thirteen places, scale is twelve
    with pytest.raises(StatementError) as caught:
        make_record(db_session, batch, "T-PRECISE", quantity=too_precise)
    assert isinstance(caught.value.orig, ValueError)
    assert "decimal places" in str(caught.value.orig)

    db_session.rollback()
    assert db_session.query(Record).filter_by(reference="T-PRECISE").count() == 0


def test_scale_boundary_is_stored_exactly(
    db_session: Session, seeded_sources: SeededSources
) -> None:
    """TR-206. Twelve places is inside the column; the refusal above is a limit,
    not a habit of rejecting precise numbers."""
    batch = make_batch(db_session, seeded_sources.ledger.id, content_hash="e" * 64)
    exact = Decimal("0.123456789012")
    record = make_record(db_session, batch, "T-EDGE", quantity=exact)
    db_session.commit()
    db_session.expire_all()
    loaded = db_session.get(Record, record.id)
    assert loaded is not None
    assert loaded.quantity == exact


# ---------------------------------------------------------------------------
# TR-506 -- timezone-aware UTC or nothing
# ---------------------------------------------------------------------------


def test_utc_only(db_session: Session, seeded_sources: SeededSources) -> None:
    """TR-506. A naive timestamp is refused; an aware one comes back as UTC.

    A naive datetime is a timestamp whose meaning depends on who reads it. Two
    systems five hours apart is not a rounding artefact, it is a break that
    never happened, so the type refuses the input rather than guessing.
    """
    batch = make_batch(db_session, seeded_sources.ledger.id, content_hash="f" * 64)

    naive = datetime(2025, 7, 1, 14, 0)
    with pytest.raises(StatementError) as caught:
        make_record(db_session, batch, "T-NAIVE", occurred_at=naive)
    assert isinstance(caught.value.orig, ValueError)
    assert "naive" in str(caught.value.orig)
    db_session.rollback()

    # The rollback discarded the batch the failed record was headed for, so the
    # aware half of the test needs its own.
    batch = make_batch(db_session, seeded_sources.ledger.id, content_hash="0" * 64)
    kolkata = timezone(timedelta(hours=5, minutes=30))
    elsewhere = datetime(2025, 7, 1, 14, 0, tzinfo=kolkata)
    record = make_record(db_session, batch, "T-AWARE", occurred_at=elsewhere)
    db_session.commit()
    db_session.expire_all()

    loaded = db_session.get(Record, record.id)
    assert loaded is not None
    assert loaded.occurred_at.tzinfo is not None, "came back naive"
    assert loaded.occurred_at.utcoffset() == timedelta(0), "came back in a non-UTC zone"
    assert loaded.occurred_at == elsewhere, "the instant changed"
    assert loaded.occurred_at == datetime(2025, 7, 1, 8, 30, tzinfo=UTC)


def test_utc_only_refuses_a_date_where_a_datetime_belongs(
    db_session: Session, seeded_sources: SeededSources
) -> None:
    """TR-506. A ``date`` has no time and no zone; it is not a timestamp."""
    batch = make_batch(db_session, seeded_sources.ledger.id, content_hash="1" * 64)
    with pytest.raises(StatementError) as caught:
        make_record(db_session, batch, "T-DATE", occurred_at=date(2025, 7, 1))
    assert isinstance(caught.value.orig, TypeError)


# ---------------------------------------------------------------------------
# Constraints. Several requirements are satisfied by the schema, not by code.
# ---------------------------------------------------------------------------


def test_duplicate_batch_hash_refused(db_session: Session, seeded_sources: SeededSources) -> None:
    """TR-103, TR-503. A byte-identical resend cannot be accepted twice.

    The ingest service checks for this too, and its check is what produces the
    message naming the original acceptance. This test is about what happens when
    that check is bypassed -- two uploads racing, or a future endpoint written by
    somebody who did not read the service.
    """
    digest = "2" * 64
    make_batch(db_session, seeded_sources.ledger.id, content_hash=digest)
    with pytest.raises(IntegrityError):
        make_batch(
            db_session,
            seeded_sources.ledger.id,
            content_hash=digest,
            filename="resent_with_a_new_name.csv",
        )
    db_session.rollback()


def test_same_hash_from_a_different_source_is_allowed(
    db_session: Session, seeded_sources: SeededSources
) -> None:
    """TR-103. The constraint is per source. Two counterparties sending
    identical bytes is a coincidence, not a duplicate delivery."""
    digest = "3" * 64
    make_batch(db_session, seeded_sources.ledger.id, content_hash=digest)
    make_batch(db_session, seeded_sources.statement.id, content_hash=digest)
    db_session.commit()
    assert db_session.query(FileBatch).filter_by(content_hash=digest).count() == 2


def _pair(run: Run, left: Record, right: Record) -> Pair:
    return Pair(
        run_id=run.id,
        left_record_id=left.id,
        right_record_id=right.id,
        origin="reference",
        verdict="agreed",
    )


def test_pair_uniqueness(db_session: Session, seeded_sources: SeededSources) -> None:
    """TR-311, TR-504. A record is in at most one pair per run, on either side.

    ``core.match`` guarantees this in its result. The constraint guarantees it
    against everything else: a manual pair created through the web layer, a
    re-run writing over a previous one, a bug. One record in two pairs is
    double-counted in the summary and hides a genuine unmatched row.
    """
    run = make_run(db_session, seeded_sources)
    ledger_batch = make_batch(db_session, seeded_sources.ledger.id, content_hash="4" * 64)
    statement_batch = make_batch(db_session, seeded_sources.statement.id, content_hash="5" * 64)

    left = make_record(db_session, ledger_batch, "T-1", row_no=1)
    right = make_record(db_session, statement_batch, "T-1", row_no=1)
    other_right = make_record(db_session, statement_batch, "T-2", row_no=2)

    db_session.add(_pair(run, left, right))
    db_session.flush()

    with pytest.raises(IntegrityError):
        db_session.add(_pair(run, left, other_right))
        db_session.flush()
    db_session.rollback()


def test_pair_uniqueness_on_the_right_side(
    db_session: Session, seeded_sources: SeededSources
) -> None:
    """TR-311, TR-504. The same rule, from the counterparty's direction."""
    run = make_run(db_session, seeded_sources)
    ledger_batch = make_batch(db_session, seeded_sources.ledger.id, content_hash="6" * 64)
    statement_batch = make_batch(db_session, seeded_sources.statement.id, content_hash="7" * 64)

    left = make_record(db_session, ledger_batch, "T-1", row_no=1)
    right = make_record(db_session, statement_batch, "T-1", row_no=1)
    other_left = make_record(db_session, ledger_batch, "T-2", row_no=2)

    db_session.add(_pair(run, left, right))
    db_session.flush()

    with pytest.raises(IntegrityError):
        db_session.add(_pair(run, other_left, right))
        db_session.flush()
    db_session.rollback()


def test_a_record_may_pair_again_in_a_later_run(
    db_session: Session, seeded_sources: SeededSources
) -> None:
    """TR-502. The constraint is scoped to the run. Re-running tomorrow pairs
    the same records again, and must not collide with today's answer."""
    ledger_batch = make_batch(db_session, seeded_sources.ledger.id, content_hash="8" * 64)
    statement_batch = make_batch(db_session, seeded_sources.statement.id, content_hash="9" * 64)
    left = make_record(db_session, ledger_batch, "T-1", row_no=1)
    right = make_record(db_session, statement_batch, "T-1", row_no=1)

    for _ in range(2):
        run = make_run(db_session, seeded_sources)
        db_session.add(_pair(run, left, right))
        db_session.flush()
    db_session.commit()

    assert db_session.query(Pair).filter_by(left_record_id=left.id).count() == 2


def test_run_item_uniqueness(db_session: Session, seeded_sources: SeededSources) -> None:
    """TR-509. One row per record per run, carrying exactly one state.

    This is what makes "every record read is in exactly one state" a property of
    the schema rather than a claim about the reconcile service. Two rows for one
    record means the state counts no longer sum to the records read, and the run
    summary silently stops adding up.
    """
    run = make_run(db_session, seeded_sources)
    batch = make_batch(db_session, seeded_sources.ledger.id, content_hash="a1" * 32)
    record = make_record(db_session, batch, "T-1")

    db_session.add(RunItem(run_id=run.id, record_id=record.id, side="left", state="agreed"))
    db_session.flush()

    with pytest.raises(IntegrityError):
        db_session.add(RunItem(run_id=run.id, record_id=record.id, side="left", state="break"))
        db_session.flush()
    db_session.rollback()


def test_foreign_keys_are_enforced(db_session: Session) -> None:
    """The constraint tests above are only meaningful if the database is
    actually enforcing what the schema declares. SQLite ignores foreign keys
    unless each connection asks it not to, which ``app.db.session`` does."""
    with pytest.raises(IntegrityError):
        make_batch(db_session, source_id=99999, content_hash="ff" * 32)
    db_session.rollback()


# ---------------------------------------------------------------------------
# TR-507 -- the schema a reviewer actually gets
# ---------------------------------------------------------------------------


def test_alembic_upgrade_head(tmp_path: Path) -> None:
    """TR-507. ``alembic upgrade head`` builds the models' schema from nothing.

    The fixtures above build their schema with ``create_all``, which is the one
    place CLAUDE.md invariant 5 allows it -- but it means the suite could be
    green against a schema no reviewer will ever have. This runs the path they
    do run, on an empty file, and compares the result to the models.
    """
    database = tmp_path / "fresh.db"
    environment = {**os.environ, "DATABASE_URL": f"sqlite:///{database}"}

    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert database.exists(), "migration ran but produced no database"

    migrated = create_engine(f"sqlite:///{database}", poolclass=StaticPool)
    try:
        tables = set(inspect(migrated).get_table_names())
    finally:
        migrated.dispose()

    expected = set(Base.metadata.tables) | {"alembic_version"}
    assert tables == expected, f"missing {expected - tables}, unexpected {tables - expected}"


def test_migration_reproduces_the_models_columns(tmp_path: Path) -> None:
    """TR-507. Same table names is not the same schema. Compare the columns too,
    so a model changed without a migration is caught here rather than in
    production."""
    database = tmp_path / "fresh.db"
    environment = {**os.environ, "DATABASE_URL": f"sqlite:///{database}"}
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr

    migrated = create_engine(f"sqlite:///{database}", poolclass=StaticPool)
    try:
        inspector = inspect(migrated)
        for name, table in Base.metadata.tables.items():
            got = {c["name"] for c in inspector.get_columns(name)}
            assert got == set(table.columns.keys()), f"{name} differs from the model"
    finally:
        migrated.dispose()


def test_exact_decimal_refuses_a_float_at_the_type_boundary() -> None:
    """TR-505. Below the ORM, the type itself is the thing that refuses.

    Worth asserting directly: every test above reaches this code through a
    flush, and a type that only refuses when the ORM happens to call it is not a
    boundary.
    """
    money = ExactDecimal(scale=12)
    with pytest.raises(TypeError, match="float"):
        money.process_bind_param(1.5, None)  # type: ignore[arg-type]
