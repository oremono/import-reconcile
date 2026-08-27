"""Fixtures shared by the whole integration suite.

Three things every integration test wants and none should build for itself:

``engine``          a throwaway SQLite file, built once per test session
``db_session``      a session whose every write is undone when the test ends
``seeded_sources``  the two sources and the tolerance profile between them

The schema here comes from ``Base.metadata.create_all`` because a fixture is the
one place CLAUDE.md invariant 5 allows it. That the migrations produce the same
schema is not assumed -- ``test_types.py`` runs ``alembic upgrade head`` against
an empty database and compares the result to the models.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any, NamedTuple

import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session

import app.db.session as session_module
from app.db.models import Base, Source, ToleranceProfile

# Importing app.db.session is what registers the connect hook that turns on
# foreign key enforcement, so the schema's declared integrity rules actually
# bind during tests rather than being decoration.


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    """A fresh SQLite file under pytest's tmp directory, never the repo's own.

    A test suite that writes to ``reconcile.db`` destroys whatever the reviewer
    was looking at, and starts from whatever the last run happened to leave.
    """
    path = tmp_path_factory.mktemp("db") / "test.db"
    test_engine = create_engine(f"sqlite:///{path}")

    _use_real_transactions(test_engine)
    Base.metadata.create_all(test_engine)
    try:
        yield test_engine
    finally:
        test_engine.dispose()


def _use_real_transactions(target: Engine) -> None:
    """Make SAVEPOINT behave, so ``db_session`` can undo a committed test.

    The sqlite3 driver opens a transaction implicitly, and only ever in front of
    a data-modifying statement -- never in front of ``SAVEPOINT``. A savepoint
    taken outside a transaction therefore starts its own, and releasing it
    *commits*, so the test's data outlives the rollback that was supposed to
    discard it. Left alone, one test's rows show up in the next one's counts.

    The fix is SQLAlchemy's documented one: stop the driver managing
    transactions, and open them explicitly. It applies to this test engine only;
    the application's engine is untouched.
    """

    @event.listens_for(target, "connect")
    def _stop_driver_managing_transactions(dbapi_connection: Any, record: Any) -> None:
        dbapi_connection.isolation_level = None

    @event.listens_for(target, "begin")
    def _begin_explicitly(connection: Any) -> None:
        connection.exec_driver_sql("BEGIN")


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    """A session inside an outer transaction that is always rolled back.

    The session joins that transaction through a SAVEPOINT, which means a test
    may ``commit()`` and see its own data, and may ``rollback()`` after a
    provoked ``IntegrityError`` and carry on -- both behave exactly as they
    would in production. When the test ends the outer transaction is rolled
    back and the database is as it was, so tests cannot depend on each other's
    leftovers.
    """
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


@pytest.fixture(scope="session", autouse=True)
def _application_engine_points_at_the_test_database(engine: Engine) -> Iterator[None]:
    """Redirect ``app.db.session`` at the temporary database.

    Anything reaching for ``get_session()`` rather than taking a session as an
    argument would otherwise open the repository's ``reconcile.db``. That is a
    design smell worth catching in review, but it is not worth letting it
    scribble on the reviewer's data while we wait.
    """
    original = session_module.engine
    session_module.engine = engine
    session_module.SessionLocal.configure(bind=engine)
    try:
        yield
    finally:
        session_module.engine = original
        session_module.SessionLocal.configure(bind=original)


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------


class SeededSources(NamedTuple):
    """The two sides of a reconciliation and the tolerances between them."""

    ledger: Source
    statement: Source
    profile: ToleranceProfile


# Column maps for the sample files in ``data/``. Enough for a test that needs a
# plausible configured source; ``app/sources.py`` is the authority for the real
# ones, and a test that cares about parsing should use those.
LEDGER_FORMAT: dict[str, Any] = {
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

STATEMENT_FORMAT: dict[str, Any] = {
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

# SPEC section 5.5, stated once. A test that hard-codes a threshold of its own
# will keep passing after the tolerance is changed, which is the failure mode
# TR-405 exists to prevent.
QUANTITY_BPS = Decimal("1")
PRICE_BPS = Decimal("5")
AMOUNT_BPS = Decimal("5")
AMOUNT_ABS_FLOOR = Decimal("0.01")
TIME_TOLERANCE_SECONDS = 300  # 5 minutes
SUGGEST_WINDOW_SECONDS = 7200  # 2 hours, SPEC D5


@pytest.fixture
def seeded_sources(db_session: Session) -> SeededSources:
    """A ledger, a statement, and the tolerance profile between them."""
    ledger = Source(code="ledger", name="Our trade ledger", format_config=LEDGER_FORMAT)
    statement = Source(
        code="statement", name="Counterparty statement", format_config=STATEMENT_FORMAT
    )
    db_session.add_all([ledger, statement])
    db_session.flush()

    profile = ToleranceProfile(
        left_source_id=ledger.id,
        right_source_id=statement.id,
        amount_bps=AMOUNT_BPS,
        amount_abs_floor=AMOUNT_ABS_FLOOR,
        price_bps=PRICE_BPS,
        qty_bps=QUANTITY_BPS,
        time_tolerance_seconds=TIME_TOLERANCE_SECONDS,
        suggest_window_seconds=SUGGEST_WINDOW_SECONDS,
    )
    db_session.add(profile)
    # Committed, not flushed. A test that provokes an IntegrityError has to roll
    # back to carry on, and rolling back to before its own fixtures would leave
    # it with dangling foreign keys. The commit releases the savepoint the
    # session joined on, so the seed survives a rollback inside the test and
    # still vanishes when the outer transaction is discarded.
    db_session.commit()
    return SeededSources(ledger=ledger, statement=statement, profile=profile)
