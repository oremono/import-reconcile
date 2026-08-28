"""Every route in DESIGN section 7, and the view models the templates render.

Three rules shape this module.

**Server-rendered only (TR-601).** Jinja templates, one stylesheet inlined in
the layout, no script tag anywhere, no asset build step.

**Every mutation is POST-then-redirect (TR-603).** A handler that changes
anything answers ``303`` pointing at a GET. A refresh re-runs the GET, never the
POST, so no analyst has ever double-loaded a statement by pressing F5. Messages
travel in the query string of that redirect rather than in a session cookie,
because a cookie would be a second piece of state to get wrong for one line of
text.

**An expected failure is a message, not a 5xx (TR-605).** A duplicate file, an
overlapping period and an unreadable file are all things a counterparty does on
an ordinary Tuesday. Each is caught here and rendered on the form the analyst
was just using. A missing run, pair or record is a 404 page.

Both collaborating services -- ``app.services.reconcile`` and
``app.services.resolve`` -- raise exceptions that are *messages*
(``ReconcileError``, ``ResolutionError``, ``IngestError``). Every route that can
provoke one catches it and redirects with the text. Anything else that escapes
is a real fault and should be a 500, loudly.

"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse, Response

from app.config import settings
from app.db.models import (
    FieldDiffRow,
    FileBatch,
    Pair,
    Record,
    RejectedRow,
    Resolution,
    Run,
    RunItem,
    Source,
)
from app.db.session import session_dependency
from app.services.ingest import IngestError, ingest_file
from app.services.reconcile import (
    ReconcileError,
    candidates_for,
    run_reconciliation,
    run_summary,
)
from app.services.reconcile import pair_detail as load_pair_detail
from app.services.reconcile import worklist as load_worklist
from app.services.resolve import (
    ResolutionError,
    accept_unmatched,
    manual_match,
    reject_suggestion,
    resolution_for,
)
from app.services.resolve import record_history as load_record_history
from core.compare import format_decimal, format_duration, format_timestamp
from core.model import (
    COMPARED_FIELDS,
    WORKLIST_STATES,
    PairOrigin,
    RecordState,
    ResolutionKind,
)

# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _trimmed(value: Decimal) -> Decimal:
    """Drop storage padding without losing a digit that means anything.

    Every money column is stored at scale 12 so the arithmetic is exact, which
    is right, and renders as ``34000.000000000000``, which is not: ten trailing
    zeros are noise an analyst has to read past on the one page whose whole job
    is making a difference obvious. This trims them and keeps two decimal
    places, so 34,000 reads as money and 0.57285 keeps all five of its digits.

    Decimal throughout. Nothing here converts to float (CLAUDE.md invariant 1).
    """
    try:
        if value == value.to_integral_value():
            return value.quantize(_CENTS)
        trimmed = value.normalize()
        exponent = trimmed.as_tuple().exponent
        if isinstance(exponent, int) and exponent > -2:
            return trimmed.quantize(_CENTS)
        return trimmed
    except InvalidOperation:  # pragma: no cover - defensive, needs a 28-digit amount
        return value


_CENTS = Decimal("0.01")


def _duration(value: Decimal | None) -> str:
    return "" if value is None else format_duration(value)


def _decimal(value: Decimal | None) -> str:
    return "" if value is None else format_decimal(_trimmed(value))


def _number(value: str) -> str:
    """Same trimming for a value already stored as text on ``field_diff``.

    Instrument, side, status and timestamps are not numbers and come back
    untouched.
    """
    try:
        return format_decimal(_trimmed(Decimal(value)))
    except InvalidOperation:
        return value


def _percent(value: Decimal | None) -> str:
    """A relative difference as a percentage, trimmed to something readable.

    ``rel_diff`` is a fraction: five basis points is ``0.0005``. Analysts read
    percentages, so it is multiplied here and only here.
    """
    if value is None:
        return ""
    scaled = value * 100
    text = format(scaled.quantize(Decimal("0.0001")), "f").rstrip("0").rstrip(".")
    return f"{text or '0'}%"


def _timestamp(value: datetime | None) -> str:
    return "" if value is None else format_timestamp(value)


def _words(value: str) -> str:
    """``agreed_with_drift`` -> ``agreed with drift``. Labels, not identifiers."""
    return value.replace("_", " ")


TEMPLATES.env.filters["dec"] = _decimal
TEMPLATES.env.filters["num"] = _number
TEMPLATES.env.filters["pct"] = _percent
TEMPLATES.env.filters["ts"] = _timestamp
TEMPLATES.env.filters["dur"] = _duration
TEMPLATES.env.filters["words"] = _words

router = APIRouter()

# ``Annotated`` rather than a call in a default argument: a default is evaluated
# once at import, which is a real bug for anything mutable and a lint failure
# either way. FastAPI reads the marker out of the annotation just the same.
SessionDep = Annotated[Session, Depends(session_dependency)]
FormField = Annotated[str, Form()]


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


def redirect_with_message(url: str, message: str | None = None, level: str = "info") -> Response:
    """POST answers with a redirect to a GET, always (TR-603).

    ``303`` and not ``302``: a browser re-issuing the original method after a
    redirect is exactly the double submission this rule exists to prevent.
    """
    if message:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{urlencode({'msg': message, 'level': level})}"
    return RedirectResponse(url, status_code=HTTPStatus.SEE_OTHER)


def error_page(request: Request, status_code: int, detail: str) -> Response:
    """A refused request as a page. Used by the handlers in ``app.main``."""
    try:
        title = HTTPStatus(status_code).phrase
    except ValueError:  # pragma: no cover - defensive
        title = "Error"
    return TEMPLATES.TemplateResponse(
        request,
        "error.html",
        {"status_code": status_code, "title": title, "detail": detail},
        status_code=status_code,
    )


def _render(request: Request, template: str, context: dict[str, Any]) -> Response:
    return TEMPLATES.TemplateResponse(request, template, context)


def _flash(msg: str | None, level: str) -> dict[str, str] | None:
    return {"text": msg, "level": level} if msg else None


def _safe_return_to(value: str | None, fallback: str) -> str:
    """Only ever redirect back inside this application."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return fallback


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def _sources(session: Session) -> list[Source]:
    return list(session.scalars(select(Source).order_by(Source.id)))


def _source_or_message(session: Session, code: str) -> Source:
    source = session.scalar(select(Source).where(Source.code == code))
    if source is None:
        known = ", ".join(s.code for s in _sources(session)) or "none configured"
        raise IngestError(f"Unknown source {code!r}. Configured sources: {known}.")
    return source


def _source_or_404(session: Session, code: str) -> Source:
    source = session.scalar(select(Source).where(Source.code == code))
    if source is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, f"No source named {code!r}.")
    return source


def _run_or_404(session: Session, run_id: int) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, f"No run {run_id}.")
    return run


def _record_or_404(session: Session, record_id: int) -> Record:
    record = session.get(Record, record_id)
    if record is None:
        raise HTTPException(HTTPStatus.NOT_FOUND, f"No record {record_id}.")
    return record


def _pair_or_404(session: Session, run_id: int, pair_id: int) -> Pair:
    pair = session.get(Pair, pair_id)
    if pair is None or pair.run_id != run_id:
        raise HTTPException(HTTPStatus.NOT_FOUND, f"No pair {pair_id} in run {run_id}.")
    return pair


def _parse_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise IngestError(f"{label} is not a date: {value!r}. Use YYYY-MM-DD.") from exc


# ---------------------------------------------------------------------------
# Period defaults
# ---------------------------------------------------------------------------

_PERIOD_IN_FILENAME = re.compile(r"(\d{4})-(\d{2})-(\d{2})_(\d{2})")


def period_from_filename(filename: str) -> tuple[date, date] | None:
    """``statement_2025-07-01_07.csv`` -> 1 July to 7 July 2025.

    The counterparty stamps the period into the name, so the analyst should not
    have to retype it. Only ever a default: whatever the form says wins.
    """
    found = _PERIOD_IN_FILENAME.search(filename)
    if found is None:
        return None
    year, month, first, last = (int(part) for part in found.groups())
    try:
        return date(year, month, first), date(year, month, last)
    except ValueError:
        return None


def default_period(session: Session) -> tuple[date, date]:
    """What to pre-fill the period boxes with.

    The last period actually loaded, because the next file is nearly always for
    the same one or the one after. Failing that, the period the sample files in
    ``data/`` cover, so a reviewer's first upload needs no typing. Failing that,
    the week just gone.
    """
    latest = session.scalar(select(FileBatch).order_by(FileBatch.id.desc()))
    if latest is not None:
        return latest.period_start, latest.period_end

    data_dir = settings.data_dir
    if data_dir.is_dir():
        for path in sorted(data_dir.glob("*.csv")):
            period = period_from_filename(path.name)
            if period is not None:
                return period

    today = datetime.now(UTC).date()
    return today - timedelta(days=7), today - timedelta(days=1)


# ---------------------------------------------------------------------------
# View models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunLine:
    """One row of the runs list on the home page."""

    run: Run
    left: str
    right: str
    worklist_total: int
    breaks: int
    agreed: int


@dataclass(frozen=True, slots=True)
class BatchLine:
    """One accepted delivery on the home page."""

    batch: FileBatch
    is_current: bool
    superseded_by_version: int | None
    rejected_rows: list[RejectedRow] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class WorklistLine:
    """One *decision*, with enough on it to triage without opening it.

    One decision, not one record. A break is a statement about a pair, and the
    run stores it twice -- once per record, because every record read must
    carry exactly one state (TR-509). Rendering both rows would show the
    analyst twelve lines for six decisions and open the same page from each.
    ``record_ids`` keeps both, so nothing is hidden by the collapse.
    """

    item: RunItem
    record: Record
    counterpart: Record | None
    pair: Pair | None
    state: str
    headline: str
    relative_size: Decimal
    absolute_size: Decimal
    href: str
    record_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class HistoryLine:
    """One version of one record, and the delivery it arrived in."""

    batch: FileBatch
    record: Record
    is_current: bool


# ---------------------------------------------------------------------------
# Summary arithmetic
# ---------------------------------------------------------------------------

#: The three groups the analyst is here for, in the order SPEC section 6.2 lists
#: them, followed by the states that only sometimes exist.
FOCUS_STATES: tuple[RecordState, ...] = (
    RecordState.BREAK,
    RecordState.SUGGESTED,
    RecordState.UNMATCHED,
    RecordState.STATUS_DISAGREEMENT,
    RecordState.WITHDRAWN,
    RecordState.REJECTED_ROW,
)

#: Everything else. True, useful, and deliberately quiet.
CONTEXT_STATES: tuple[RecordState, ...] = (
    RecordState.AGREED,
    RecordState.AGREED_WITH_DRIFT,
    RecordState.ACCEPTED_UNMATCHED,
    RecordState.EXCLUDED,
)


def worklist_total(counts: dict[str, int]) -> int:
    """Items needing a person. The one number that belongs at the top."""
    return sum(counts.get(state.value, 0) for state in WORKLIST_STATES)


def _counts_by_side(session: Session, run_id: int) -> dict[tuple[str, str], int]:
    rows = session.execute(
        select(RunItem.side, RunItem.state, func.count(RunItem.id))
        .where(RunItem.run_id == run_id)
        .group_by(RunItem.side, RunItem.state)
    )
    return {(side, state): count for side, state, count in rows}


def _side_totals(by_side: dict[tuple[str, str], int]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for (side, _state), count in by_side.items():
        totals[side] = totals.get(side, 0) + count
    return totals


def _unmatched_by_side(by_side: dict[tuple[str, str], int]) -> dict[str, int]:
    return {
        side: count
        for (side, state), count in by_side.items()
        if state == RecordState.UNMATCHED.value
    }


# ---------------------------------------------------------------------------
# Worklist assembly
# ---------------------------------------------------------------------------

#: Money fields, largest-first, for choosing which difference to headline.
_HEADLINE_FIELDS = ("gross_amount", "unit_price", "quantity", "occurred_at")


def _diffs_for_pairs(session: Session, pair_ids: Sequence[int]) -> dict[int, list[FieldDiffRow]]:
    if not pair_ids:
        return {}
    rows = session.scalars(select(FieldDiffRow).where(FieldDiffRow.pair_id.in_(pair_ids)))
    grouped: dict[int, list[FieldDiffRow]] = {}
    for row in rows:
        grouped.setdefault(row.pair_id, []).append(row)
    return grouped


def _leading_diff(diffs: Sequence[FieldDiffRow]) -> FieldDiffRow | None:
    """The difference worth putting on the list line.

    Largest relative difference wins, because that is what the list sorts by and
    a line that headlines a different number than it is ordered by is a lie.
    Ties break towards money.
    """
    differing = [row for row in diffs if row.differs]
    if not differing:
        return None
    order = {name: index for index, name in enumerate(_HEADLINE_FIELDS)}
    return max(
        differing,
        key=lambda row: (
            row.rel_diff if row.rel_diff is not None else Decimal(0),
            -order.get(row.field, len(order)),
        ),
    )


def _magnitude(field: str, value: Decimal) -> str:
    """A difference with its units attached.

    ``occurred_at`` is compared in seconds, so a forty-minute gap comes back as
    ``2400``. Printed bare next to a column of amounts it reads as money, and
    printed at storage scale it reads as ``2400.000000000000``, which is worse.
    """
    if field == OCCURRED_AT:
        return format_duration(value)
    return _decimal(value)


def _values(field: str, left: str, right: str) -> str:
    """The two sides of a difference, without repeating what they share.

    Two timestamps on the same day differ in the part after the date, so
    printing the date twice pushes the part that actually differs off the end
    of the cell. Same day, one date.
    """
    if field == OCCURRED_AT and left[:10] == right[:10] and left.endswith(" UTC"):
        return f"{left[:-4]} vs {right[11:]}"
    return f"{_number(left)} vs {_number(right)}"


_STATE_HEADLINES = {
    RecordState.UNMATCHED.value: "No counterpart on the other side",
    RecordState.WITHDRAWN.value: "Present in an earlier version of the file, gone now",
    RecordState.REJECTED_ROW.value: "Row could not be read",
    RecordState.STATUS_DISAGREEMENT.value: "Paired, but the two sides disagree on status",
    RecordState.SUGGESTED.value: "A plausible counterpart is waiting for confirmation",
}


def _headline(item: RunItem, diffs: Sequence[FieldDiffRow]) -> tuple[str, Decimal]:
    leading = _leading_diff(diffs)
    if leading is not None:
        magnitude = leading.rel_diff if leading.rel_diff is not None else Decimal(0)
        text = (
            f"{_words(leading.field)}: "
            f"{_values(leading.field, leading.left_value, leading.right_value)}"
        )
        if leading.abs_diff is not None:
            text += f", off by {_magnitude(leading.field, abs(leading.abs_diff))}"
        return text, magnitude
    return _STATE_HEADLINES.get(item.state, _words(item.state)), Decimal(0)


def _absolute_size(record: Record, diffs: Sequence[FieldDiffRow]) -> Decimal:
    """Money at stake, so the biggest problems can be brought to the top.

    The gap between the two gross amounts when they disagree. Otherwise the
    whole trade: if the two sides agree on the money but disagree on the side,
    the status or the time, it is the entire amount that is in question, not
    nothing. Taking the largest difference across every field would put a
    forty-minute timestamp gap in a money column as "2400", which is a number
    in the wrong units pretending to be a number in the right ones.
    """
    for row in diffs:
        if row.field == GROSS_AMOUNT and row.differs and row.abs_diff is not None:
            return abs(row.abs_diff)
    return abs(record.gross_amount)


GROSS_AMOUNT = "gross_amount"
OCCURRED_AT = "occurred_at"


def build_worklist(session: Session, run_id: int, items: Sequence[RunItem]) -> list[WorklistLine]:
    """Turn run items into list lines, resolving records and diffs in two queries."""
    record_ids = [item.record_id for item in items]
    records = (
        {
            record.id: record
            for record in session.scalars(select(Record).where(Record.id.in_(record_ids)))
        }
        if record_ids
        else {}
    )

    pair_ids = [item.pair_id for item in items if item.pair_id is not None]
    pairs = (
        {pair.id: pair for pair in session.scalars(select(Pair).where(Pair.id.in_(pair_ids)))}
        if pair_ids
        else {}
    )
    diffs = _diffs_for_pairs(session, pair_ids)

    # Both sides of a pair are the same decision. Keep the left one, which is
    # ours, and remember the other so the line can name it.
    by_pair: dict[int, list[RunItem]] = {}
    for item in items:
        if item.pair_id is not None:
            by_pair.setdefault(item.pair_id, []).append(item)

    lines: list[WorklistLine] = []
    seen_pairs: set[int] = set()
    for item in items:
        record = records.get(item.record_id)
        if record is None:  # pragma: no cover - a foreign key makes this impossible
            continue

        pair = pairs.get(item.pair_id) if item.pair_id is not None else None
        if pair is not None:
            if pair.id in seen_pairs:
                continue
            seen_pairs.add(pair.id)
            siblings = by_pair.get(pair.id, [item])
            ours = next((s for s in siblings if s.side == LEFT_SIDE), siblings[0])
            item = ours
            record = records.get(ours.record_id, record)
            other_id = (
                pair.right_record_id if record.id == pair.left_record_id else pair.left_record_id
            )
            counterpart = records.get(other_id) or session.get(Record, other_id)
            covered = tuple(s.record_id for s in siblings)
        else:
            counterpart = None
            covered = (record.id,)

        rows = diffs.get(item.pair_id, []) if item.pair_id is not None else []
        headline, relative = _headline(item, rows)
        if pair is not None:
            relative = max(relative, pair.max_rel_diff)
        href = (
            f"/runs/{run_id}/pairs/{pair.id}"
            if pair is not None
            else f"/runs/{run_id}/records/{record.id}"
        )
        lines.append(
            WorklistLine(
                item=item,
                record=record,
                counterpart=counterpart,
                pair=pair,
                state=item.state,
                headline=headline,
                relative_size=relative,
                absolute_size=_absolute_size(record, rows),
                href=href,
                record_ids=covered,
            )
        )
    return lines


#: ``run_item.side`` for our own ledger. Named rather than repeated as a literal.
LEFT_SIDE = "left"


SORTS: dict[str, str] = {
    "relative": "Relative difference",
    "absolute": "Money at stake",
    "reference": "Reference",
}


def sort_worklist(lines: list[WorklistLine], sort: str) -> list[WorklistLine]:
    """Largest difference first. TR-606.

    Two readings of "size of difference" are both right and neither subsumes the
    other: a 40% error on a small trade and a small error on a large one are
    different problems. The analyst picks; relative is the default because a
    percentage is comparable across instruments.
    """
    if sort == "reference":
        return sorted(lines, key=lambda line: (line.record.reference, line.item.id))
    if sort == "absolute":
        return sorted(lines, key=lambda line: (-line.absolute_size, line.record.reference))
    return sorted(
        lines, key=lambda line: (-line.relative_size, -line.absolute_size, line.record.reference)
    )


# ---------------------------------------------------------------------------
# GET /
# ---------------------------------------------------------------------------


@router.get("/")
def home(
    request: Request,
    session: SessionDep,
    msg: str | None = None,
    level: str = "info",
) -> Response:
    """Runs, the upload form, and every delivery accepted so far (SPEC 6.1)."""
    sources = _sources(session)
    names = {source.id: source.code for source in sources}

    runs = list(session.scalars(select(Run).order_by(Run.id.desc()).limit(50)))
    run_lines = []
    for run in runs:
        counts = run_summary(session, run.id)
        run_lines.append(
            RunLine(
                run=run,
                left=names.get(run.left_source_id, "?"),
                right=names.get(run.right_source_id, "?"),
                worklist_total=worklist_total(counts),
                breaks=counts.get(RecordState.BREAK.value, 0),
                agreed=counts.get(RecordState.AGREED.value, 0)
                + counts.get(RecordState.AGREED_WITH_DRIFT.value, 0),
            )
        )

    batches = list(session.scalars(select(FileBatch).order_by(FileBatch.id.desc())))
    versions = {batch.id: batch.version_no for batch in batches}
    rejected = _rejected_rows_by_batch(session, [batch.id for batch in batches])
    batch_lines = [
        BatchLine(
            batch=batch,
            is_current=batch.superseded_by_id is None,
            superseded_by_version=versions.get(batch.superseded_by_id or -1),
            rejected_rows=rejected.get(batch.id, []),
        )
        for batch in batches
    ]

    period_start, period_end = default_period(session)
    return _render(
        request,
        "home.html",
        {
            "flash": _flash(msg, level),
            "sources": sources,
            "source_names": names,
            "runs": run_lines,
            "batches": batch_lines,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
        },
    )


def _rejected_rows_by_batch(
    session: Session, batch_ids: Sequence[int]
) -> dict[int, list[RejectedRow]]:
    if not batch_ids:
        return {}
    rows = session.scalars(select(RejectedRow).where(RejectedRow.batch_id.in_(batch_ids)))
    grouped: dict[int, list[RejectedRow]] = {}
    for row in rows:
        grouped.setdefault(row.batch_id, []).append(row)
    return grouped


# ---------------------------------------------------------------------------
# POST /files
# ---------------------------------------------------------------------------


@router.post("/files")
async def upload_file(
    session: SessionDep,
    upload: Annotated[UploadFile, File()],
    source: FormField,
    period_start: FormField = "",
    period_end: FormField = "",
) -> Response:
    """Accept a delivery, or say in one sentence why it cannot be accepted.

    Every refusal below is something a counterparty does routinely: a resend, a
    period that does not line up, a file that is not the CSV we agreed. None of
    them is a server fault and none of them produces a 5xx (TR-605).
    """
    try:
        record_source = _source_or_message(session, source)
        content = await upload.read()
        filename = upload.filename or "upload.csv"
        if not content:
            raise IngestError(f"{filename} is empty. Nothing was loaded.")

        start, end = _period_for_upload(period_start, period_end, filename)
        if end < start:
            raise IngestError(f"Period ends before it starts: {start} to {end}.")

        result = ingest_file(session, record_source, content, start, end, filename)
    except IngestError as exc:
        # Nothing was written -- the guards run before the first insert -- but
        # rolling back makes that true regardless of where the refusal came from.
        session.rollback()
        return redirect_with_message("/", str(exc), level="error")

    message = result.summary
    if result.withdrawn_references:
        message += f" Withdrawn: {', '.join(result.withdrawn_references)}."
    return redirect_with_message("/", message, level="success")


def _period_for_upload(period_start: str, period_end: str, filename: str) -> tuple[date, date]:
    """The form wins; the filename is the fallback when the form is blank."""
    if period_start.strip() and period_end.strip():
        return (
            _parse_date(period_start, "Period start"),
            _parse_date(period_end, "Period end"),
        )
    from_name = period_from_filename(filename)
    if from_name is None:
        raise IngestError(
            f"A period is required, and {filename!r} does not carry one in its name. "
            "Fill in both dates and try again."
        )
    return from_name


# ---------------------------------------------------------------------------
# POST /runs
# ---------------------------------------------------------------------------


@router.post("/runs")
def start_run(
    session: SessionDep,
    left_source: FormField,
    right_source: FormField,
    period_start: FormField,
    period_end: FormField,
) -> Response:
    """Reconcile two sources over a period, then open the summary (Flow 1)."""
    try:
        left = _source_or_message(session, left_source)
        right = _source_or_message(session, right_source)
        if left.id == right.id:
            raise IngestError("A run compares two different sources.")
        start = _parse_date(period_start, "Period start")
        end = _parse_date(period_end, "Period end")
        run = run_reconciliation(session, left, right, start, end)
    except (IngestError, ReconcileError) as exc:
        session.rollback()
        return redirect_with_message("/", str(exc), level="error")
    except (LookupError, ValueError) as exc:
        # A missing tolerance profile or an unconfigured pair is a setup problem
        # the analyst can fix, not a crash.
        session.rollback()
        return redirect_with_message("/", f"That run could not start: {exc}", level="error")

    return redirect_with_message(f"/runs/{run.id}", "Run complete.", level="success")


# ---------------------------------------------------------------------------
# GET /runs/{run_id}
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}")
def run_detail(
    request: Request,
    run_id: int,
    session: SessionDep,
    msg: str | None = None,
    level: str = "info",
) -> Response:
    """How bad is today, in one glance (SPEC 6.2)."""
    run = _run_or_404(session, run_id)

    counts: dict[str, int] = dict(run_summary(session, run_id))
    by_side = _counts_by_side(session, run_id)
    names = {source.id: source for source in _sources(session)}

    return _render(
        request,
        "run.html",
        {
            "flash": _flash(msg, level),
            "run": run,
            "left": names.get(run.left_source_id),
            "right": names.get(run.right_source_id),
            "counts": counts,
            "worklist_total": worklist_total(counts),
            "focus_states": [(s.value, counts.get(s.value, 0)) for s in FOCUS_STATES],
            "context_states": [(s.value, counts.get(s.value, 0)) for s in CONTEXT_STATES],
            "side_totals": _side_totals(by_side),
            "unmatched_by_side": _unmatched_by_side(by_side),
        },
    )


# ---------------------------------------------------------------------------
# GET /runs/{run_id}/worklist
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}/worklist")
def run_worklist(
    request: Request,
    run_id: int,
    session: SessionDep,
    state: Annotated[str | None, Query()] = None,
    sort: Annotated[str, Query()] = "relative",
    msg: str | None = None,
    level: str = "info",
) -> Response:
    """Everything needing a decision, filtered and ordered by size (TR-606)."""
    run = _run_or_404(session, run_id)
    wanted = state or None
    if wanted is not None and wanted not in {s.value for s in RecordState}:
        raise HTTPException(HTTPStatus.NOT_FOUND, f"No such state {wanted!r}.")
    if sort not in SORTS:
        sort = "relative"

    items: list[RunItem] = list(load_worklist(session, run_id, wanted))
    lines = sort_worklist(build_worklist(session, run_id, items), sort)
    # The same numbers the summary shows, from the same place. Rejected rows are
    # counted there and cannot be listed here -- a row that never parsed has no
    # record to point at -- so the page says where they are instead.
    counts = run_summary(session, run_id)

    return _render(
        request,
        "worklist.html",
        {
            "flash": _flash(msg, level),
            "run": run,
            "lines": lines,
            "state": wanted,
            "sort": sort,
            "sorts": SORTS,
            "states": [(s.value, counts.get(s.value, 0)) for s in FOCUS_STATES],
            "records_total": worklist_total(counts),
            "rejected_rows": counts.get(RecordState.REJECTED_ROW.value, 0),
        },
    )


# ---------------------------------------------------------------------------
# GET /runs/{run_id}/pairs/{pair_id}
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}/pairs/{pair_id}")
def pair_detail(
    request: Request,
    run_id: int,
    pair_id: int,
    session: SessionDep,
    msg: str | None = None,
    level: str = "info",
) -> Response:
    """One pair, field by field, both values and both differences (TR-607)."""
    run = _run_or_404(session, run_id)
    pair = _pair_or_404(session, run_id, pair_id)

    _pair, left_record, right_record, diff_rows = load_pair_detail(session, pair_id)
    rows: list[FieldDiffRow] = list(diff_rows)

    order = {name: index for index, name in enumerate(COMPARED_FIELDS)}
    rows.sort(key=lambda row: order.get(row.field, len(order)))
    resolution = _provenance(session, pair, left_record, right_record)
    names = {source.id: source for source in _sources(session)}

    return _render(
        request,
        "pair.html",
        {
            "flash": _flash(msg, level),
            "run": run,
            "pair": pair,
            "left_record": left_record,
            "right_record": right_record,
            "left_source": names.get(left_record.source_id),
            "right_source": names.get(right_record.source_id),
            "rows": rows,
            "differing": [row for row in rows if row.differs],
            "resolution": resolution,
            "kinds": ResolutionKind,
        },
    )


def _provenance(session: Session, pair: Pair, left: Record, right: Record) -> Resolution | None:
    """The decision behind a manual pair.

    ``pair.resolution_id`` when the run recorded one. Otherwise the live
    resolution naming these two records, looked up the way every resolution is
    keyed -- on ``(source, reference)``, never on a record id, because a
    correction writes new record rows and a row-id lookup would come back empty
    exactly when the analyst most wants to know who paired these (invariant 4).
    """
    if pair.resolution_id is not None:
        return session.get(Resolution, pair.resolution_id)
    if pair.origin != PairOrigin.MANUAL.value:
        return None
    return session.scalar(
        select(Resolution)
        .where(
            Resolution.kind == ResolutionKind.MANUAL_MATCH.value,
            Resolution.left_source_id == left.source_id,
            Resolution.left_reference == left.reference,
            Resolution.right_source_id == right.source_id,
            Resolution.right_reference == right.reference,
            Resolution.revoked_at.is_(None),
        )
        .order_by(Resolution.id.desc())
    )


# ---------------------------------------------------------------------------
# GET /runs/{run_id}/records/{record_id}
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}/records/{record_id}")
def record_detail(
    request: Request,
    run_id: int,
    record_id: int,
    session: SessionDep,
    msg: str | None = None,
    level: str = "info",
) -> Response:
    """One record with no counterpart, its ranked candidates, and both actions.

    TR-608 wants the reason each candidate qualifies, not just a list: a
    candidate offered without a reason asks the analyst to re-derive the
    ranking by eye, which is the work the page was supposed to save.
    """
    run = _run_or_404(session, run_id)
    record = _record_or_404(session, record_id)

    candidates: list[tuple[Record, str]] = list(candidates_for(session, run_id, record_id))
    names = {source.id: source for source in _sources(session)}
    this_source = names.get(record.source_id)
    other_id = run.right_source_id if record.source_id == run.left_source_id else run.left_source_id
    other_source = names.get(other_id)
    on_left = record.source_id == run.left_source_id

    item = session.scalar(
        select(RunItem).where(RunItem.run_id == run_id, RunItem.record_id == record_id)
    )

    # A decision recorded since this run finished is not visible in its
    # run_item, so the page would otherwise show a green "paired" banner above
    # a badge insisting nothing has been paired. The decision is what is true
    # now; the state is what was true when the run executed.
    decision = resolution_for(session, this_source, record.reference) if this_source else None
    if decision is not None and decision.revoked_at is not None:
        decision = None
    decision_is_newer = decision is not None and (
        run.finished_at is None or decision.created_at > run.finished_at
    )

    return _render(
        request,
        "record.html",
        {
            "flash": _flash(msg, level),
            "run": run,
            "record": record,
            "item": item,
            "decision": decision,
            "decision_is_newer": decision_is_newer,
            "this_source": this_source,
            "other_source": other_source,
            "left_source": names.get(run.left_source_id),
            "right_source": names.get(run.right_source_id),
            "on_left": on_left,
            "candidates": candidates,
            "return_to": f"/runs/{run_id}/records/{record_id}",
            "fields": COMPARED_FIELDS,
        },
    )


# ---------------------------------------------------------------------------
# POST /resolutions
# ---------------------------------------------------------------------------


@router.post("/resolutions")
def create_resolution(
    session: SessionDep,
    kind: FormField,
    reason: FormField = "",
    author: FormField = "",
    left_source: FormField = "",
    left_reference: FormField = "",
    right_source: FormField = "",
    right_reference: FormField = "",
    return_to: FormField = "/",
) -> Response:
    """Record a durable decision. Reason and author are mandatory (TR-604).

    Both fields carry ``required`` in the markup, which stops the ordinary
    mistake in the browser. This check is the one that counts: a form posted by
    anything other than that page reaches the same rule.
    """
    target = _safe_return_to(return_to, "/")
    reason = reason.strip()
    author = author.strip()

    if not reason or not author:
        missing = " and ".join(
            label for label, value in (("a reason", reason), ("an author", author)) if not value
        )
        return redirect_with_message(
            target,
            f"Nothing was recorded: every resolution needs {missing}.",
            level="error",
        )

    try:
        result = _dispatch_resolution(
            session,
            kind,
            left_source,
            left_reference,
            right_source,
            right_reference,
            reason,
            author,
        )
    except (IngestError, ResolutionError) as exc:
        session.rollback()
        return redirect_with_message(target, f"Nothing was recorded: {exc}.", level="error")
    except (LookupError, ValueError) as exc:
        session.rollback()
        return redirect_with_message(target, f"That could not be recorded: {exc}", level="error")

    return redirect_with_message(target, result, level="success")


def _dispatch_resolution(
    session: Session,
    kind: str,
    left_source: str,
    left_reference: str,
    right_source: str,
    right_reference: str,
    reason: str,
    author: str,
) -> str:
    """One of the three decisions in SPEC 5.7, or a message saying why not."""
    left_reference = left_reference.strip()
    right_reference = right_reference.strip()

    if kind == ResolutionKind.ACCEPT_UNMATCHED.value:
        if not left_reference:
            raise IngestError("Nothing was recorded: no record was named.")
        source = _source_or_message(session, left_source)
        accept_unmatched(session, source, left_reference, reason, author)
        return f"{left_reference} accepted as having no counterpart."

    if kind in {ResolutionKind.MANUAL_MATCH.value, ResolutionKind.REJECT_SUGGESTION.value}:
        if not left_reference or not right_reference:
            raise IngestError("Nothing was recorded: a pairing needs a reference on both sides.")
        left = _source_or_message(session, left_source)
        right = _source_or_message(session, right_source)
        pairing = kind == ResolutionKind.MANUAL_MATCH.value
        record_it = manual_match if pairing else reject_suggestion
        record_it(session, left, left_reference, right, right_reference, reason, author)
        verb = "paired with" if pairing else "rejected as a counterpart for"
        return f"{left_reference} {verb} {right_reference}. Future runs will honour it."

    raise IngestError(f"Nothing was recorded: {kind!r} is not a kind of resolution.")


# ---------------------------------------------------------------------------
# GET /records/{source}/{reference}/history
# ---------------------------------------------------------------------------


@router.get("/records/{source_code}/{reference}/history")
def record_history(
    request: Request,
    source_code: str,
    reference: str,
    session: SessionDep,
    msg: str | None = None,
    level: str = "info",
) -> Response:
    """What this row said before, which file version said it, and every decision on it."""
    source = _source_or_404(session, source_code)

    history = [
        HistoryLine(batch=batch, record=record, is_current=batch.superseded_by_id is None)
        for batch, record in load_record_history(session, source, reference)
    ]
    resolutions = list(
        session.scalars(
            select(Resolution)
            .where(
                (
                    (Resolution.left_source_id == source.id)
                    & (Resolution.left_reference == reference)
                )
                | (
                    (Resolution.right_source_id == source.id)
                    & (Resolution.right_reference == reference)
                )
            )
            .order_by(Resolution.id)
        )
    )

    return _render(
        request,
        "history.html",
        {
            "flash": _flash(msg, level),
            "source": source,
            "reference": reference,
            "history": history,
            "resolutions": resolutions,
            "fields": COMPARED_FIELDS,
        },
    )
