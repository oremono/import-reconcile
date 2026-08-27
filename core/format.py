"""Building a validated :class:`SourceFormat` from stored configuration.

Everything that differs between two systems sending the same data lives in
``source.format_config`` as JSON (CLAUDE.md invariant 8). This module is the one
place that JSON is inspected: it is checked once, at load, so a misconfigured
source fails at startup rather than as ten thousand row errors discovered
halfway through a run (DESIGN.md section 8, TR-203).

Standard library only. See CLAUDE.md invariant 2.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from core.model import RecordStatus, Side, SourceFormat

# The common field set every source is mapped onto. See SPEC.md R2.1.
REQUIRED_FIELDS: tuple[str, ...] = (
    "reference",
    "occurred_at",
    "instrument",
    "side",
    "quantity",
    "unit_price",
    "gross_amount",
    "status",
)

#: The pattern that means "this column holds epoch seconds" rather than a
#: ``strptime`` pattern. ``strptime`` has no portable directive for it.
EPOCH_SECONDS: str = "%s"


class FormatConfigError(ValueError):
    """A stored source configuration that cannot be trusted to load rows.

    Raised at configuration time, never per row: a row error means one bad row,
    this means the source itself is not describable.
    """


def source_format_from_config(source_code: str, config: Mapping[str, Any]) -> SourceFormat:
    """Validate ``config`` and return the ``SourceFormat`` it describes.

    ``config`` is the JSON stored on ``source.format_config``. Recognised keys
    are named exactly as the :class:`SourceFormat` fields they fill:
    ``columns``, ``timestamp_formats``, ``timezone``, ``side_map``,
    ``status_map``. Unrecognised top-level keys are ignored so the envelope can
    grow; unrecognised keys inside ``columns`` are rejected, because that field
    set is closed and a typo there would otherwise read as a missing mapping.

    Raises:
        FormatConfigError: on any defect that would make normalisation
            unreliable - a missing field mapping, an absent or unknown
            timezone, an empty pattern list, or a vocabulary entry that does not
            name a known ``Side`` or ``RecordStatus``.
    """
    code = _non_empty_str(source_code, "source_code")
    if not isinstance(config, Mapping):
        raise FormatConfigError(
            f"{code}: format configuration must be an object, got {_kind(config)}"
        )

    columns = _columns(code, config)
    timestamp_formats = _timestamp_formats(code, config)
    timezone = _timezone(code, config)
    side_map = _vocabulary(code, config, "side_map", Side)
    status_map = _vocabulary(code, config, "status_map", RecordStatus)

    return SourceFormat(
        source_code=code,
        columns=columns,
        timestamp_formats=timestamp_formats,
        timezone=timezone,
        side_map=side_map,
        status_map=status_map,
    )


# ---------------------------------------------------------------------------
# Per-key validation
# ---------------------------------------------------------------------------


def _columns(code: str, config: Mapping[str, Any]) -> Mapping[str, str]:
    raw = config.get("columns")
    if raw is None:
        raise FormatConfigError(f"{code}: 'columns' is required")
    if not isinstance(raw, Mapping):
        raise FormatConfigError(f"{code}: 'columns' must be an object, got {_kind(raw)}")

    columns: dict[str, str] = {}
    for field, column in raw.items():
        if not isinstance(field, str):
            raise FormatConfigError(f"{code}: 'columns' keys must be strings, got {_kind(field)}")
        if not isinstance(column, str) or not column.strip():
            raise FormatConfigError(
                f"{code}: 'columns[{field}]' must be a non-empty column name, got {column!r}"
            )
        columns[field] = column.strip()

    missing = [field for field in REQUIRED_FIELDS if field not in columns]
    if missing:
        raise FormatConfigError(f"{code}: 'columns' is missing a mapping for {_names(missing)}")

    unknown = sorted(set(columns) - set(REQUIRED_FIELDS))
    if unknown:
        raise FormatConfigError(
            f"{code}: 'columns' names unknown field {_names(unknown)}; "
            f"known fields are {_names(REQUIRED_FIELDS)}"
        )
    return columns


def _timestamp_formats(code: str, config: Mapping[str, Any]) -> tuple[str, ...]:
    raw = config.get("timestamp_formats")
    if raw is None:
        raise FormatConfigError(f"{code}: 'timestamp_formats' is required")
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise FormatConfigError(
            f"{code}: 'timestamp_formats' must be a list of patterns, got {_kind(raw)}"
        )
    patterns = tuple(raw)
    if not patterns:
        raise FormatConfigError(
            f"{code}: 'timestamp_formats' is empty; a source must declare at least one pattern"
        )
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern.strip():
            raise FormatConfigError(
                f"{code}: 'timestamp_formats' entries must be non-empty strings, got {pattern!r}"
            )
    return tuple(pattern.strip() for pattern in patterns)


def _timezone(code: str, config: Mapping[str, Any]) -> str:
    raw = config.get("timezone")
    if raw is None:
        raise FormatConfigError(
            f"{code}: 'timezone' is required; a source's timezone is declared, never guessed "
            f"per file (SPEC.md D17)"
        )
    if not isinstance(raw, str) or not raw.strip():
        raise FormatConfigError(f"{code}: 'timezone' must be an IANA zone name, got {raw!r}")
    name = raw.strip()
    try:
        ZoneInfo(name)
    except (ValueError, KeyError, OSError) as exc:
        raise FormatConfigError(f"{code}: 'timezone' {name!r} is not a known IANA zone") from exc
    return name


def _vocabulary[EnumT: StrEnum](
    code: str,
    config: Mapping[str, Any],
    key: str,
    vocabulary: type[EnumT],
) -> Mapping[str, EnumT]:
    raw = config.get(key)
    if raw is None:
        raise FormatConfigError(f"{code}: {key!r} is required")
    if not isinstance(raw, Mapping):
        raise FormatConfigError(f"{code}: {key!r} must be an object, got {_kind(raw)}")
    if not raw:
        raise FormatConfigError(f"{code}: {key!r} is empty; declare the tokens this source sends")

    known = _names(vocabulary.__members__)
    mapped: dict[str, EnumT] = {}
    for token, value in raw.items():
        if not isinstance(token, str) or not token.strip():
            raise FormatConfigError(
                f"{code}: {key!r} keys must be non-empty strings, got {token!r}"
            )
        if not isinstance(value, str):
            raise FormatConfigError(
                f"{code}: {key}[{token!r}] must name one of {known}, got {_kind(value)}"
            )
        try:
            mapped[token.strip()] = vocabulary(value.strip().upper())
        except ValueError as exc:
            raise FormatConfigError(
                f"{code}: {key}[{token!r}] maps to {value!r}, which is not one of {known}"
            ) from exc
    return mapped


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _non_empty_str(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FormatConfigError(f"{label} must be a non-empty string, got {value!r}")
    return value.strip()


def _names(values: Iterable[str]) -> str:
    return ", ".join(repr(value) for value in values)


def _kind(value: object) -> str:
    return type(value).__name__
