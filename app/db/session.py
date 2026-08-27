"""The engine, the session factory, and the one place a transaction is opened.

Two rules shape this module.

**The backend is chosen by URL alone (TR-704).** Nothing here reads which
database it got. ``settings.database_url`` decides, and every statement below is
written once for both backends.

**Schema comes from Alembic (TR-507).** This module connects to a database that
already exists; it never builds one. Test fixtures build their own.

The single unavoidable backend-specific fact is that SQLite ignores foreign keys
unless each connection asks it not to. That is handled below by recognising the
driver's own connection object rather than by asking SQLAlchemy which dialect is
in play -- a driver is a capability, a dialect name is a branch.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings

engine: Engine = create_engine(settings.database_url, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


@event.listens_for(Engine, "connect")
def _enforce_referential_integrity(dbapi_connection: Any, connection_record: Any) -> None:
    """Ask a connection to enforce foreign keys when it is capable of ignoring them.

    SQLite defaults ``foreign_keys`` to off, per connection, for backwards
    compatibility. A schema whose integrity rules are declared but not enforced
    is worse than one with no rules at all, so every connection that can be
    asked, is asked.

    The test is ``isinstance(..., sqlite3.Connection)``: the driver's own type,
    from the standard library, on the object the driver just handed us. No
    dialect is named and no code path elsewhere learns which backend it got, so
    TR-704 holds. Every other driver enforces foreign keys unconditionally and
    falls straight through.

    Registered against the ``Engine`` class rather than one engine, so engines
    built elsewhere -- Alembic's, the test fixtures' -- get the same treatment.
    Forgetting it in one place is how a test passes against rules the
    application does not actually enforce.
    """
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def session_dependency() -> Iterator[Session]:
    """Yield a session, committing on success and rolling back on any exception.

    Written as a plain generator so FastAPI can use it directly as a dependency
    (``Depends(session_dependency)``). ``get_session`` below is the same thing
    for callers outside a request.

    The commit lives here rather than in the services so that one request or one
    command is one transaction. A service that commits mid-way leaves a partial
    ingest visible, which is the failure TR-108 exists to prevent.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


get_session = contextmanager(session_dependency)
"""``with get_session() as session:`` for services, scripts and tests."""
