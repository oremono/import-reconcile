"""The ASGI application.

Thin on purpose. Everything the web surface does lives in ``app.web.routes``;
this module exists so ``uvicorn app.main:app`` has something to import and so
the failure modes that must never be a stack trace are registered in one place
(TR-605).

Two handlers are installed:

``HTTPException``   a missing run, pair or record renders as a page saying so,
                    with the status the caller asked for. 404, not a traceback.
``IngestError``     a file that cannot be accepted is a message, never a 500.
                    Routes catch it themselves so they can put the message back
                    on the form the analyst was using; this handler is the
                    backstop for any path that forgets.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from app.observability import configure_logging, get_logger
from app.services.ingest import IngestError
from app.web.routes import error_page, redirect_with_message, router

configure_logging()

SETUP_INSTRUCTIONS = (
    "This database has no schema yet. From the project directory run "
    "`uv run alembic upgrade head` and then `uv run python -m app.seed`, "
    "and reload this page."
)


def schema_is_missing(error: BaseException) -> bool:
    """Is this the "you skipped a setup step" failure, rather than a real fault?

    SQLite creates an empty file the moment anything connects, so a database
    that was never migrated does not announce itself: the server starts, and
    then every page fails deep inside a query. Postgres says "does not exist"
    where SQLite says "no such table"; matching both keeps this from being a
    branch on which backend is in play (TR-704).
    """
    text = str(getattr(error, "orig", error)).lower()
    return "no such table" in text or "does not exist" in text


app = FastAPI(title="import-reconcile", docs_url=None, redoc_url=None)
app.include_router(router)


@app.exception_handler(OperationalError)
def _render_database_error(request: Request, exc: OperationalError) -> Response:
    """A missing schema is a setup step, not a server fault. Say which one."""
    if schema_is_missing(exc):
        get_logger().error("database has no schema; run alembic upgrade head")
        return error_page(request, 503, SETUP_INSTRUCTIONS)
    return error_page(
        request,
        503,
        "The database could not be reached. Check DATABASE_URL and try again.",
    )


@app.exception_handler(StarletteHTTPException)
def _render_http_exception(request: Request, exc: StarletteHTTPException) -> Response:
    """A missing run or pair is a page, not a traceback (TR-605)."""
    return error_page(request, exc.status_code, str(exc.detail))


@app.exception_handler(RequestValidationError)
def _render_validation_error(request: Request, exc: RequestValidationError) -> Response:
    """A malformed form submission is the analyst's typo, not a server fault."""
    return error_page(
        request,
        400,
        "That form could not be read. Please check the fields and try again.",
    )


@app.exception_handler(IngestError)
def _render_ingest_error(request: Request, exc: IngestError) -> Response:
    """Backstop for TR-605. The routes catch this first; nothing should reach here."""
    return redirect_with_message("/", str(exc), level="error")
