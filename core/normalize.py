"""Turning one source's raw CSV row into our own vocabulary.

Pure functions over dictionaries: no file handles, no database, no source names.
Every detail that varies between systems arrives in the :class:`SourceFormat`
argument (CLAUDE.md invariant 8, TR-201), so reconciling a third counterparty is
one configuration row and no change here (TR-207).

A row that cannot be normalised is *returned* as a :class:`RowError`, never
raised: one unreadable row must not stop the rows around it from loading
(SPEC.md R1.5, TR-106).

Standard library only. See CLAUDE.md invariant 2.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from core.format import EPOCH_SECONDS, REQUIRED_FIELDS
from core.model import NormalizedRecord, RecordStatus, RowError, Side, SourceFormat

#: Characters a system may put inside a number for legibility. Removing them
#: changes no digit, so precision survives (TR-206).
_GROUPING = (" ", "\u202f", "\u00a0", "_", ",")

#: Unix epoch as an aware instant. Epoch columns are offsets from here.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

_MICROSECONDS = Decimal(1_000_000)

#: A byte-order mark leading the first header cell is an encoding artefact, not
#: part of the column's name.
_BOM = "\ufeff"


class HeaderError(ValueError):
    """A file whose header does not carry every column the source maps.

    Raised before any data row is read, so a mis-delivered file is refused
    whole rather than row by row (TR-101).
    """


class _RowRejected(Exception):
    """Internal: the first defect found in a row, carrying its explanation."""


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------


def validate_header(header: Sequence[str], fmt: SourceFormat) -> None:
    """Check that ``header`` carries every column ``fmt`` maps a field onto.

    Raises:
        HeaderError: naming the missing columns, and the fields that wanted
            them, so the message is actionable without opening the config.
    """
    present = {_clean_header_cell(cell) for cell in header}
    missing = sorted(
        f"{column!r} (for {field})"
        for field, column in fmt.columns.items()
        if column not in present
    )
    if missing:
        raise HeaderError(
            f"{fmt.source_code}: file header is missing {', '.join(missing)}; "
            f"header has {sorted(present)}"
        )


def _clean_header_cell(cell: str) -> str:
    return cell.lstrip(_BOM).strip()


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def normalize_row(
    raw: Mapping[str, str],
    fmt: SourceFormat,
    row_no: int,
) -> NormalizedRecord | RowError:
    """Normalise one row, or explain why it cannot be.

    Returns a :class:`NormalizedRecord` on success and a :class:`RowError` on
    any failure. It does not raise: the caller is loading a file and a single
    bad row is data, not an exception.
    """
    snapshot: Mapping[str, str] = {key: value for key, value in raw.items()}
    try:
        return NormalizedRecord(
            source_code=fmt.source_code,
            reference=_text(raw, fmt, "reference"),
            occurred_at=_timestamp(_text(raw, fmt, "occurred_at"), fmt),
            instrument=_text(raw, fmt, "instrument"),
            side=_token(_text(raw, fmt, "side"), fmt.side_map, "side"),
            quantity=_number(_text(raw, fmt, "quantity"), "quantity"),
            unit_price=_number(_text(raw, fmt, "unit_price"), "unit_price"),
            gross_amount=_number(_text(raw, fmt, "gross_amount"), "gross_amount"),
            status=_token(_text(raw, fmt, "status"), fmt.status_map, "status"),
            row_no=row_no,
            raw=snapshot,
        )
    except _RowRejected as exc:
        return RowError(row_no=row_no, reason=str(exc), raw=snapshot)
    except Exception as exc:  # a bad row must never stop the file
        return RowError(
            row_no=row_no, reason=f"unexpected {type(exc).__name__}: {exc}", raw=snapshot
        )


def normalize_rows(
    rows: Iterable[Mapping[str, str]],
    fmt: SourceFormat,
    *,
    start_row_no: int = 2,
) -> tuple[tuple[NormalizedRecord, ...], tuple[RowError, ...]]:
    """Normalise every row, keeping the good ones and the reasons for the rest.

    ``start_row_no`` defaults to 2 so a row number is the line number a person
    sees in their spreadsheet, the header being line 1.
    """
    records: list[NormalizedRecord] = []
    errors: list[RowError] = []
    for offset, row in enumerate(rows):
        outcome = normalize_row(row, fmt, start_row_no + offset)
        if isinstance(outcome, RowError):
            errors.append(outcome)
        else:
            records.append(outcome)
    return tuple(records), tuple(errors)


# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------


def _text(raw: Mapping[str, str], fmt: SourceFormat, field: str) -> str:
    """The stripped value of ``field``'s column, or a rejection if it is absent."""
    column = fmt.columns.get(field)
    if column is None:
        raise _RowRejected(f"source {fmt.source_code!r} maps no column onto {field!r}")
    if column not in raw:
        raise _RowRejected(f"missing column {column!r} for {field}")
    value = raw[column]
    if value is None:
        raise _RowRejected(f"missing value in column {column!r} for {field}")
    text = value.strip()
    if not text:
        raise _RowRejected(f"empty value in column {column!r} for {field}")
    return text


def _number(text: str, field: str) -> Decimal:
    """Parse an exact decimal, preserving every digit the source sent.

    No rounding, no quantising, no ``float`` anywhere near it: whatever
    precision arrived is the precision stored, and differences are settled by
    tolerance at comparison time (SPEC.md R2.5, TR-205, TR-206).
    """
    cleaned = text
    for separator in _GROUPING:
        cleaned = cleaned.replace(separator, "")
    try:
        value = Decimal(str(cleaned))
    except (InvalidOperation, ValueError) as exc:
        raise _RowRejected(f"{field} {text!r} is not a number") from exc
    if not value.is_finite():
        raise _RowRejected(f"{field} {text!r} is not a finite number")
    return value


def _token[VocabT: (Side, RecordStatus)](
    text: str,
    vocabulary: Mapping[str, VocabT],
    field: str,
) -> VocabT:
    """Map a source's own coded value through its declared vocabulary.

    An unmapped token is a row error naming the token. There is deliberately no
    default and no pass-through: silently guessing a side is how a buy becomes a
    sell (SPEC.md R2.3, TR-204).
    """
    mapped = vocabulary.get(text)
    if mapped is None:
        raise _RowRejected(f"unmapped {field} token {text!r}; source declares {sorted(vocabulary)}")
    return mapped


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def _timestamp(text: str, fmt: SourceFormat) -> datetime:
    """Parse a timestamp and return it as an aware UTC instant.

    Patterns are tried in the declared order and the first that parses wins;
    exhausting them is a row error (TR-202). A value carrying no offset is
    localised with the source's declared timezone and converted to UTC - never
    guessed per file (SPEC.md D17, TR-203).
    """
    for pattern in fmt.timestamp_formats:
        parsed = _parse(text, pattern)
        if parsed is not None:
            return _to_utc(parsed, text, fmt)
    raise _RowRejected(f"timestamp {text!r} is not in this source's format ({_example(fmt)})")


def _example(fmt: SourceFormat) -> str:
    """What this source's timestamps look like, as an example rather than a pattern.

    A rejected row is read by an analyst deciding whether to chase the
    counterparty, not by the person who wrote the config. ``%Y-%m-%dT%H:%M:%SZ``
    tells them nothing; ``2025-07-01T09:15:00Z`` tells them everything.
    """
    sample = datetime(2025, 7, 1, 9, 15, tzinfo=UTC)
    for pattern in fmt.timestamp_formats:
        if pattern == EPOCH_SECONDS:
            return "epoch seconds, e.g. 1751361300"
        try:
            return f"e.g. {sample.strftime(pattern)}"
        except ValueError:  # pragma: no cover - a pattern strftime cannot render
            continue
    return "no format configured"


def _parse(text: str, pattern: str) -> datetime | None:
    """One attempt. ``None`` means this pattern did not fit; try the next."""
    if pattern == EPOCH_SECONDS:
        return _from_epoch(text)
    try:
        return datetime.strptime(text, pattern)
    except ValueError:
        pass
    # A trailing "Z" is an explicit offset. Accept it even where the pattern
    # does not spell it out, rather than failing a row over a suffix.
    if text.endswith(("Z", "z")):
        try:
            return datetime.strptime(text[:-1], pattern)
        except ValueError:
            return None
    return None


def _from_epoch(text: str) -> datetime | None:
    """Epoch seconds, converted without a float ever holding the value."""
    try:
        seconds = Decimal(str(text))
    except (InvalidOperation, ValueError):
        return None
    if not seconds.is_finite():
        return None
    whole = int(seconds)
    micros = int(((seconds - whole) * _MICROSECONDS).to_integral_value())
    try:
        return _EPOCH + timedelta(seconds=whole, microseconds=micros)
    except (OverflowError, OSError):
        return None


def _to_utc(parsed: datetime, text: str, fmt: SourceFormat) -> datetime:
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)
    if text.endswith(("Z", "z")):
        return parsed.replace(tzinfo=UTC)
    try:
        zone = ZoneInfo(fmt.timezone)
    except (ValueError, KeyError, OSError) as exc:
        raise _RowRejected(
            f"source {fmt.source_code!r} declares timezone {fmt.timezone!r}, which is not a "
            f"known IANA zone"
        ) from exc
    return parsed.replace(tzinfo=zone).astimezone(UTC)


__all__ = [
    "EPOCH_SECONDS",
    "REQUIRED_FIELDS",
    "HeaderError",
    "normalize_row",
    "normalize_rows",
    "validate_header",
]
