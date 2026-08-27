"""Durable decisions: recording what a person decided, so nobody decides it twice.

SPEC section 5.7 states the rule this module exists to make true: *a decision
made by a person must never need to be made twice.* Everything here follows
from one choice, and it is worth being explicit about why.

**A resolution names identities, never rows.** ``record`` rows are immutable, so
a correction writes *new* rows for the same transaction under a new batch. A
resolution holding ``record.id`` would therefore point at the superseded row and
silently stop applying on the very morning the counterparty resent the file --
exactly when yesterday's work matters most. Keying on ``(source_id, reference)``
makes "a resolution is a statement about identity, not about a run" (R7.4) a
property of the storage layer rather than a claim in a document. Nothing in this
module reads or stores a ``record.id``; ``tests/integration/test_durability.py``
proves the consequence rather than trusting the intent (TR-508, D9, D19, DD-5).

Two further rules, both enforced here rather than upstream:

**Reason and author are mandatory.** TR-604 puts the check on the web form, but a
service that accepts a blank reason makes the form the only thing standing
between the audit trail and an empty string. R7.3 is a rule about the decision,
so it is enforced where decisions are recorded.

**Nothing is hard-deleted** (TR-708). :func:`revoke` stamps ``revoked_at`` and
``revoked_reason``; it never issues a DELETE. A decision replaced by a later one
about the same identity is superseded the same way, with the supersession
recorded. "What did we think last Tuesday, and why did that change?" stays an
answerable question.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.orm import Session

from app.db.models import FileBatch, Record, Resolution, Source
from core.model import PriorResolution, RecordKey, ResolutionKind

__all__ = [
    "ResolutionError",
    "accept_unmatched",
    "manual_match",
    "prior_resolutions",
    "record_history",
    "reject_suggestion",
    "resolution_for",
    "revoke",
]


class ResolutionError(ValueError):
    """A decision that cannot be recorded as stated.

    Raised for a missing reason or author, an empty reference, a self-pairing,
    or a revocation of something already revoked. Every case is a message the
    web layer can render to the analyst; none of them is a 5xx (TR-605).
    """


# ---------------------------------------------------------------------------
# Identity
#
# The whole module speaks in these, and in nothing else. There is deliberately
# no function here that takes or returns a record id.
# ---------------------------------------------------------------------------


#: ``(source_id, reference)`` -- the business identity a resolution names.
Identity = tuple[int, str]


def _identity(source: Source, reference: str, label: str) -> Identity:
    """The identity of one record, validated.

    The reference is stripped because ingestion strips it (``core.normalize``),
    and a resolution keyed on ``"T-1001 "`` when the record says ``"T-1001"``
    would be inert forever without ever looking wrong.
    """
    return source.id, _required(reference, label)


def _required(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResolutionError(f"a resolution needs a non-empty {label} (SPEC R7.3, TR-604)")
    return value.strip()


def _now() -> datetime:
    """Timezone-aware UTC. ``UtcDateTime`` rejects anything else (TR-506)."""
    return datetime.now(UTC)


def _identities_of(resolution: Resolution) -> set[Identity]:
    """Both identities a stored resolution names. An acceptance names one."""
    found: set[Identity] = {(resolution.left_source_id, resolution.left_reference)}
    if resolution.right_source_id is not None and resolution.right_reference is not None:
        found.add((resolution.right_source_id, resolution.right_reference))
    return found


def _pair_of(resolution: Resolution) -> tuple[Identity, Identity] | None:
    """The ordered pair a stored resolution is about, or ``None`` for an acceptance."""
    if resolution.right_source_id is None or resolution.right_reference is None:
        return None
    return (
        (resolution.left_source_id, resolution.left_reference),
        (resolution.right_source_id, resolution.right_reference),
    )


def _touching(identities: set[Identity]) -> ColumnElement[bool]:
    """Live resolutions naming any of these identities, on either side."""
    return or_(
        *[
            or_(
                and_(
                    Resolution.left_source_id == source_id,
                    Resolution.left_reference == reference,
                ),
                and_(
                    Resolution.right_source_id == source_id,
                    Resolution.right_reference == reference,
                ),
            )
            for source_id, reference in sorted(identities)
        ]
    )


# ---------------------------------------------------------------------------
# Recording a decision
# ---------------------------------------------------------------------------


def manual_match(
    session: Session,
    left_source: Source,
    left_reference: str,
    right_source: Source,
    right_reference: str,
    reason: str,
    author: str,
) -> Resolution:
    """Pair two records a person believes describe the same transaction (R7.1).

    Pairing asserts *identity*, not agreement: the pair is compared by the same
    code path as an automatic one and may well come back a break (R7.5, TR-316).
    """
    left = _identity(left_source, left_reference, "left reference")
    right = _identity(right_source, right_reference, "right reference")
    if left == right:
        raise ResolutionError("a record cannot be paired with itself")
    return _record(session, ResolutionKind.MANUAL_MATCH, left, right, reason, author)


def accept_unmatched(
    session: Session,
    source: Source,
    reference: str,
    reason: str,
    author: str,
) -> Resolution:
    """Accept that a record genuinely has no counterpart (R7.2).

    It leaves the worklist and stays out of it on every later run -- unless a
    correction supplies the counterpart, in which case the acceptance is
    revoked and reported. That is the one automatic reversal in the system
    (R7.7, D10, TR-313), and it is decided in ``core.match``.
    """
    left = _identity(source, reference, "reference")
    return _record(session, ResolutionKind.ACCEPT_UNMATCHED, left, None, reason, author)


def reject_suggestion(
    session: Session,
    left_source: Source,
    left_reference: str,
    right_source: Source,
    right_reference: str,
    reason: str,
    author: str,
) -> Resolution:
    """Record that this suggested pair is wrong, so it is never offered again (R4.6, TR-310).

    A rejection is about one ordered pair and nothing else. It says nothing
    about either record's fate on its own, so it neither settles them nor
    blocks a different suggestion for the same left record.
    """
    left = _identity(left_source, left_reference, "left reference")
    right = _identity(right_source, right_reference, "right reference")
    if left == right:
        raise ResolutionError("a record cannot be paired with itself")
    return _record(session, ResolutionKind.REJECT_SUGGESTION, left, right, reason, author)


def _record(
    session: Session,
    kind: ResolutionKind,
    left: Identity,
    right: Identity | None,
    reason: str,
    author: str,
) -> Resolution:
    """Store one decision, retiring whatever it replaces."""
    reason = _required(reason, "reason")
    author = _required(author, "author")

    _supersede(session, kind, left, right, author)

    resolution = Resolution(
        kind=str(kind),
        left_source_id=left[0],
        left_reference=left[1],
        right_source_id=right[0] if right is not None else None,
        right_reference=right[1] if right is not None else None,
        reason=reason,
        author=author,
        created_at=_now(),
    )
    session.add(resolution)
    session.flush()
    return resolution


def _supersede(
    session: Session,
    kind: ResolutionKind,
    left: Identity,
    right: Identity | None,
    author: str,
) -> None:
    """Retire live decisions the new one contradicts, recording that it happened.

    Two live decisions about the same record would make matching depend on which
    one a query happened to return first. Rather than let that happen -- or
    delete the older row, which TR-708 forbids -- the older decision is revoked
    with a reason naming what replaced it.
    """
    identities = {left} | ({right} if right is not None else set())
    new_pair = (left, right) if right is not None else None

    for existing in _live_touching(session, identities):
        if not _contradicts(existing, kind, identities, new_pair):
            continue
        _stamp(
            existing,
            f"superseded by a later {kind} recorded by {author}",
        )


def _live_touching(session: Session, identities: set[Identity]) -> list[Resolution]:
    return list(
        session.scalars(
            select(Resolution)
            .where(Resolution.revoked_at.is_(None), _touching(identities))
            .order_by(Resolution.id)
        )
    )


def _contradicts(
    existing: Resolution,
    kind: ResolutionKind,
    identities: set[Identity],
    new_pair: tuple[Identity, Identity] | None,
) -> bool:
    """Can these two decisions both stand?

    A rejection is a statement about one ordered pair, so it only ever collides
    with another decision about that same pair. A pairing or an acceptance is a
    statement about the records themselves, so it collides with any live
    decision of either kind naming one of them.
    """
    existing_kind = ResolutionKind(existing.kind)
    rejection_involved = ResolutionKind.REJECT_SUGGESTION in (existing_kind, kind)
    if rejection_involved:
        existing_pair = _pair_of(existing)
        return existing_pair is not None and new_pair is not None and existing_pair == new_pair
    return bool(_identities_of(existing) & identities)


# ---------------------------------------------------------------------------
# Undoing one
# ---------------------------------------------------------------------------


def revoke(session: Session, resolution_id: int, reason: str) -> Resolution:
    """Withdraw a decision, keeping the row and the reason (R7.8, TR-708).

    Used both by the analyst undoing their own decision and by the reconciler
    applying the automatic revocation in R7.7. Nothing is deleted, so a run that
    happened while the decision stood is still explicable afterwards.
    """
    reason = _required(reason, "reason")
    resolution = session.get(Resolution, resolution_id)
    if resolution is None:
        raise ResolutionError(f"no resolution with id {resolution_id}")
    if resolution.revoked_at is not None:
        raise ResolutionError(
            f"resolution {resolution_id} was already revoked at "
            f"{resolution.revoked_at:%Y-%m-%d %H:%M:%S%z}: {resolution.revoked_reason}"
        )
    _stamp(resolution, reason)
    session.flush()
    return resolution


def _stamp(resolution: Resolution, reason: str) -> None:
    """The only write this module makes to an existing row, and never a DELETE."""
    resolution.revoked_at = _now()
    resolution.revoked_reason = reason


# ---------------------------------------------------------------------------
# Reading them back
# ---------------------------------------------------------------------------


def prior_resolutions(
    session: Session, left_source: Source, right_source: Source
) -> list[PriorResolution]:
    """Live decisions for this source pair, in the shape ``core.match`` consumes.

    Only live ones: a revoked decision stays in the table for the audit trail
    and stops influencing matching the moment it is revoked.

    Orientation is a property of the *run*, not of the decision (R7.4), so a
    pairing recorded when these two sources were the other way round is
    returned the way this run needs it rather than being quietly ignored.
    """
    codes = {left_source.id: left_source.code, right_source.id: right_source.code}
    rows = session.scalars(
        select(Resolution)
        .where(
            Resolution.revoked_at.is_(None),
            Resolution.left_source_id.in_(codes),
            or_(
                Resolution.right_source_id.is_(None),
                Resolution.right_source_id.in_(codes),
            ),
        )
        .order_by(Resolution.id)
    )

    carried: list[PriorResolution] = []
    for row in rows:
        kind = ResolutionKind(row.kind)
        left = RecordKey(codes[row.left_source_id], row.left_reference)

        if kind is ResolutionKind.ACCEPT_UNMATCHED:
            carried.append(PriorResolution(kind=kind, left=left, right=None))
            continue

        if row.right_source_id is None or row.right_reference is None:
            # A pairing decision missing a side cannot be applied to anything.
            continue
        right = RecordKey(codes[row.right_source_id], row.right_reference)
        if row.left_source_id == right_source.id:
            left, right = right, left
        carried.append(PriorResolution(kind=kind, left=left, right=right))
    return carried


def resolution_for(session: Session, source: Source, reference: str) -> Resolution | None:
    """The live decision governing one record, or ``None``.

    This is how a caller turns a :class:`core.model.Revocation` -- which names an
    identity, because that is all matching knows about -- back into the row to
    revoke.

    A rejection is a blocklist entry rather than a decision about a record's
    fate, so a pairing or an acceptance is preferred when both exist. Among
    equals the most recent wins, though supersession means there is normally
    only one.
    """
    live = _live_touching(session, {(source.id, _required(reference, "reference"))})
    governing = [r for r in live if ResolutionKind(r.kind) is not ResolutionKind.REJECT_SUGGESTION]
    candidates = governing or live
    return candidates[-1] if candidates else None


def record_history(
    session: Session, source: Source, reference: str
) -> list[tuple[FileBatch, Record]]:
    """Every version of one record, oldest first, in one query (TR-510, R8.2, R8.3, AC8).

    A correction never overwrites anything, so the history is already sitting in
    the table: each version is a row under its own batch. That makes "what did
    this row say before, and which file said it?" a single indexed join rather
    than a loop over batches -- which matters, because looping is how this
    quietly becomes N queries and then a performance bug nobody attributes to
    the history page.
    """
    rows = session.execute(
        select(FileBatch, Record)
        .join(Record, Record.batch_id == FileBatch.id)
        .where(Record.source_id == source.id, Record.reference == _required(reference, "reference"))
        .order_by(FileBatch.version_no, Record.row_no)
    ).all()
    return [(batch, record) for batch, record in rows]
