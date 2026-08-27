"""The shared vocabulary.

Every other module in ``core`` and every service that talks to ``core`` speaks in
these types. Frozen after Wave 0 of the build: changing a shape here invalidates
work already done against it.

Standard library only. See CLAUDE.md invariant 2.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------


class Side(StrEnum):
    """Direction of a trade, after each source's own tokens are mapped."""

    BUY = "BUY"
    SELL = "SELL"


class RecordStatus(StrEnum):
    """Lifecycle state as reported by a source, after vocabulary mapping."""

    SETTLED = "SETTLED"
    PENDING = "PENDING"
    CANCELLED = "CANCELLED"


class Verdict(StrEnum):
    """Outcome of comparing one pair. See SPEC.md R5.1, R5.2."""

    AGREED = "agreed"
    AGREED_WITH_DRIFT = "agreed_with_drift"
    BREAK = "break"


class RecordState(StrEnum):
    """The closed set a record can end a run in. See SPEC.md section 5.6.

    ``WORKLIST_STATES`` below is the subset that needs a person.
    """

    EXCLUDED = "excluded"
    AGREED = "agreed"
    AGREED_WITH_DRIFT = "agreed_with_drift"
    BREAK = "break"
    SUGGESTED = "suggested"
    UNMATCHED = "unmatched"
    STATUS_DISAGREEMENT = "status_disagreement"
    ACCEPTED_UNMATCHED = "accepted_unmatched"
    WITHDRAWN = "withdrawn"
    REJECTED_ROW = "rejected_row"


WORKLIST_STATES: frozenset[RecordState] = frozenset(
    {
        RecordState.BREAK,
        RecordState.SUGGESTED,
        RecordState.UNMATCHED,
        RecordState.STATUS_DISAGREEMENT,
        RecordState.WITHDRAWN,
        RecordState.REJECTED_ROW,
    }
)


class PairOrigin(StrEnum):
    """How a pair came to exist."""

    REFERENCE = "reference"
    SUGGESTED = "suggested"
    MANUAL = "manual"


class ResolutionKind(StrEnum):
    """The three durable decisions a person can make. See SPEC.md section 5.7."""

    MANUAL_MATCH = "manual_match"
    ACCEPT_UNMATCHED = "accept_unmatched"
    REJECT_SUGGESTION = "reject_suggestion"


# Field names used in comparison output. Order is render order.
COMPARED_FIELDS: tuple[str, ...] = (
    "occurred_at",
    "instrument",
    "side",
    "quantity",
    "unit_price",
    "gross_amount",
    "status",
)


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecordKey:
    """Business identity of a record: the source that sent it and its reference.

    Resolutions key on this, never on a database row id, because a correction
    writes new record rows. See CLAUDE.md invariant 4 and DESIGN.md DD-5.
    """

    source_code: str
    reference: str


@dataclass(frozen=True, slots=True)
class NormalizedRecord:
    """One transaction as reported by one source, in our own vocabulary.

    ``occurred_at`` is always timezone-aware UTC. Numeric fields are always
    ``Decimal`` at the precision the source sent.
    """

    source_code: str
    reference: str
    occurred_at: datetime
    instrument: str
    side: Side
    quantity: Decimal
    unit_price: Decimal
    gross_amount: Decimal
    status: RecordStatus
    row_no: int
    raw: Mapping[str, str]

    @property
    def key(self) -> RecordKey:
        return RecordKey(self.source_code, self.reference)

    @property
    def is_cancelled(self) -> bool:
        return self.status is RecordStatus.CANCELLED


@dataclass(frozen=True, slots=True)
class RowError:
    """A row that could not be normalised. Carries enough to show a person."""

    row_no: int
    reason: str
    raw: Mapping[str, str]


# --------------------------------------------------------------------------
# Source configuration
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceFormat:
    """Everything that differs between two systems sending the same data.

    Adding a third source means constructing one more of these. No source name
    appears in normalisation code. See CLAUDE.md invariant 8.
    """

    source_code: str
    columns: Mapping[str, str]
    timestamp_formats: Sequence[str]
    timezone: str
    side_map: Mapping[str, Side]
    status_map: Mapping[str, RecordStatus]


@dataclass(frozen=True, slots=True)
class Tolerances:
    """Thresholds for one source pair. See SPEC.md section 5.5 and D13.

    Basis-point values are fractions: 5 bps is ``Decimal("0.0005")``.
    """

    amount_bps: Decimal
    amount_abs_floor: Decimal
    price_bps: Decimal
    qty_bps: Decimal
    time_tolerance_seconds: int
    suggest_window_seconds: int


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FieldDiff:
    """One field of one pair, compared.

    Emitted for every compared field, including those that agree, so a detail
    page can render the whole record in one pass. ``abs_diff`` is in the field's
    own units - seconds for a timestamp, currency for an amount - and is ``None``
    for fields where a difference has no magnitude, such as instrument.
    """

    field: str
    left_value: str
    right_value: str
    differs: bool
    within_tolerance: bool
    abs_diff: Decimal | None = None
    rel_diff: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Comparison:
    """The result of comparing one pair."""

    verdict: Verdict
    diffs: tuple[FieldDiff, ...]

    @property
    def differing(self) -> tuple[FieldDiff, ...]:
        return tuple(d for d in self.diffs if d.differs)

    @property
    def out_of_tolerance(self) -> tuple[FieldDiff, ...]:
        return tuple(d for d in self.diffs if not d.within_tolerance)

    @property
    def max_rel_diff(self) -> Decimal:
        """Largest relative difference across fields. Sorts the worklist."""
        values = [d.rel_diff for d in self.diffs if d.rel_diff is not None]
        return max(values) if values else Decimal(0)


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PriorResolution:
    """A decision made on an earlier run, expressed in business identities."""

    kind: ResolutionKind
    left: RecordKey
    right: RecordKey | None = None


@dataclass(frozen=True, slots=True)
class MatchedPair:
    left: NormalizedRecord
    right: NormalizedRecord
    origin: PairOrigin


@dataclass(frozen=True, slots=True)
class Suggestion:
    """A plausible pair, offered to a person. Never applied automatically."""

    left: NormalizedRecord
    right: NormalizedRecord
    rank: int
    time_gap_seconds: Decimal
    qty_rel_diff: Decimal
    reason: str


@dataclass(frozen=True, slots=True)
class Revocation:
    """An acceptance the system withdrew because a counterpart appeared.

    The only automatic reversal in the system, and always reported.
    See SPEC.md R7.7 and D10.
    """

    key: RecordKey
    reason: str


@dataclass(frozen=True, slots=True)
class MatchResult:
    """Everything one matching pass produced. Pure output, no IO."""

    pairs: tuple[MatchedPair, ...] = ()
    suggestions: tuple[Suggestion, ...] = ()
    unmatched_left: tuple[NormalizedRecord, ...] = ()
    unmatched_right: tuple[NormalizedRecord, ...] = ()
    accepted_unmatched: tuple[NormalizedRecord, ...] = ()
    status_disagreements: tuple[NormalizedRecord, ...] = ()
    excluded_left: tuple[NormalizedRecord, ...] = ()
    excluded_right: tuple[NormalizedRecord, ...] = ()
    revocations: tuple[Revocation, ...] = ()
