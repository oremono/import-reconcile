"""Structured logging for the two events an auditor asks about. TR-706.

An ingest and a run are the only things this application does that change what
an analyst sees the next morning, so both leave a machine-readable trace: which
source, which period, which version of the file, and what it produced.

The formatter emits one JSON object per line. Stdlib ``logging`` and stdlib
``json``, because a log format is not worth a dependency, and because a line
that ``jq`` can read is the whole requirement.

Convention: everything specific to an event travels in ``extra``, never
interpolated into the message. A message with the numbers baked into it has to
be parsed back out; a field does not.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

LOGGER_NAME = "reconcile"

#: Attributes ``logging.LogRecord`` sets on every record. Anything on a record
#: that is not one of these arrived through ``extra`` and is therefore ours.
_STANDARD_FIELDS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


def _plain(value: Any) -> Any:
    """Render a value as something ``json`` will accept, without losing precision.

    ``Decimal`` becomes its exact string, never a float: a log line is evidence,
    and evidence rounded on the way to disk is not evidence (CLAUDE.md
    invariant 1).
    """
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_plain(v) for v in value]
    if isinstance(value, str | int | bool) or value is None:
        return value
    return str(value)


class JsonFormatter(logging.Formatter):
    """One JSON object per line: the standard envelope plus whatever ``extra`` carried."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_FIELDS or key.startswith("_"):
                continue
            payload[key] = _plain(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=True, default=str)


def get_logger(name: str = LOGGER_NAME) -> logging.Logger:
    """The application logger. Named, so a host can route it without guessing."""
    return logging.getLogger(name)


def configure_logging(level: str = "INFO", stream: Any = None) -> logging.Logger:
    """Attach the JSON formatter once. Safe to call twice; the second call is a no-op.

    Called from application startup. Tests capture the logger directly rather
    than through this, so importing this module never installs a handler as a
    side effect.
    """
    logger = get_logger()
    logger.setLevel(level.upper())
    if not any(getattr(h, "_reconcile_json", False) for h in logger.handlers):
        handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
        handler.setFormatter(JsonFormatter())
        handler._reconcile_json = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    return logger


# ---------------------------------------------------------------------------
# The two events
# ---------------------------------------------------------------------------

INGEST_EVENT = "ingest.accepted"
RUN_EVENT = "run.completed"


def log_ingest(
    *,
    source: str,
    period_start: object,
    period_end: object,
    version: int,
    batch_id: int,
    accepted_rows: int,
    rejected_rows: int,
    withdrawn: int = 0,
    superseded_batch_id: int | None = None,
) -> None:
    """TR-706. What arrived, for which period, as which version, and what it cost."""
    get_logger().info(
        INGEST_EVENT,
        extra={
            "source": source,
            "period_start": str(period_start),
            "period_end": str(period_end),
            "version": version,
            "batch_id": batch_id,
            "counts": {
                "accepted_rows": accepted_rows,
                "rejected_rows": rejected_rows,
                "withdrawn": withdrawn,
            },
            "superseded_batch_id": superseded_batch_id,
        },
    )


def log_run(
    *,
    run_id: int,
    left_source: str,
    right_source: str,
    period_start: object,
    period_end: object,
    counts: Mapping[str, int],
    records_read: int,
    versions: Mapping[str, int] | None = None,
    duration_seconds: Decimal | None = None,
) -> None:
    """TR-706. Which run, over which pair and period, and its state counts.

    ``counts`` is the run summary itself, so a log line and the stored summary
    can be diffed against each other without a database. ``versions`` names the
    file version each side was read at, which is what makes a run reproducible
    from its log line alone.
    """
    get_logger().info(
        RUN_EVENT,
        extra={
            "run_id": run_id,
            "source": f"{left_source}<->{right_source}",
            "left_source": left_source,
            "right_source": right_source,
            "period_start": str(period_start),
            "period_end": str(period_end),
            # A run has no version of its own; it inherits the version of each
            # file it read, which is the thing an auditor needs to reproduce it.
            "version": dict(versions) if versions else {},
            "counts": dict(counts),
            "records_read": records_read,
            "duration_seconds": duration_seconds,
        },
    )


REQUEST_EVENT = "request"

#: Health checks are the host's business, not a visit. Render polls this every
#: few seconds and logging it would bury every real request under machine noise.
UNLOGGED_PATHS = frozenset({"/healthz", "/favicon.ico"})


def visitor_prefix(client_host: str | None) -> str:
    """A coarse, stable identifier for a caller. Never the full address.

    Enough to tell one visitor from another and to spot a crawler; not enough to
    identify a person. IPv4 keeps its first three octets, IPv6 its routing
    prefix, and anything unrecognised becomes "unknown" rather than being
    logged verbatim by accident.
    """
    if not client_host:
        return "unknown"
    if ":" in client_host:
        parts = client_host.split(":")
        return ":".join(parts[:3]) + "::/48" if len(parts) >= 3 else "unknown"
    octets = client_host.split(".")
    return ".".join(octets[:3]) + ".0/24" if len(octets) == 4 else "unknown"


def log_request(
    *,
    method: str,
    path: str,
    status: int,
    duration_ms: int,
    visitor: str,
    user_agent: str | None = None,
) -> None:
    """One structured line per request, so a visit can be counted rather than read.

    The host's own request logs are a paid feature, and the server's access log
    is prose. This is the same information as a queryable object.
    """
    get_logger().info(
        "%s %s %s",
        method,
        path,
        status,
        extra={
            "event": REQUEST_EVENT,
            "method": method,
            "path": path,
            "status": status,
            "duration_ms": duration_ms,
            "visitor": visitor,
            "user_agent": (user_agent or "")[:120] or None,
        },
    )
