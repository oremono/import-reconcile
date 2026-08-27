"""Matching: deciding which record on one side describes the same transaction
as which record on the other.

A pure function of its inputs. No IO, no database, no clock - the brief
requires this logic to be testable without either, and ``test_boundaries.py``
enforces the import boundary that makes it so.

Five tiers, applied in order, each considering only what earlier tiers left:

0. Decisions a person already made, applied before anything automatic.
1. Cancelled records set aside, and one-sided cancellations surfaced.
2. Reference equality, which is the ordinary case.
3. Plausible candidates, *proposed* and never applied.
4. Whatever is left, reported on both sides.

The partition is total and disjoint: every record passed in comes back in
exactly one bucket. That is what makes the run summary add up (SPEC AC2).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import Decimal

from core.model import (
    MatchedPair,
    MatchResult,
    NormalizedRecord,
    PairOrigin,
    PriorResolution,
    RecordKey,
    ResolutionKind,
    Revocation,
    Suggestion,
    Tolerances,
)
from core.tolerance import compare_quantity, seconds_between

__all__ = ["match"]


def _by_reference(record: NormalizedRecord) -> str:
    return record.reference


def _bucket_key(record: NormalizedRecord) -> tuple[str, str]:
    """Candidates must agree on instrument and side, so only these need comparing.

    This is what keeps tier 3 away from a full cross-product (TR-314).
    """
    return record.instrument, str(record.side)


def _minutes(seconds: Decimal) -> str:
    return f"{abs(seconds) / Decimal(60):.0f} min"


def _resolution_index(
    prior: Sequence[PriorResolution],
) -> tuple[dict[RecordKey, RecordKey], set[RecordKey], set[tuple[RecordKey, RecordKey]]]:
    """Split stored decisions into the three things matching does with them."""
    manual: dict[RecordKey, RecordKey] = {}
    accepted: set[RecordKey] = set()
    rejected: set[tuple[RecordKey, RecordKey]] = set()

    for resolution in prior:
        if resolution.kind is ResolutionKind.MANUAL_MATCH and resolution.right is not None:
            manual[resolution.left] = resolution.right
        elif resolution.kind is ResolutionKind.ACCEPT_UNMATCHED:
            accepted.add(resolution.left)
        elif resolution.kind is ResolutionKind.REJECT_SUGGESTION and resolution.right is not None:
            rejected.add((resolution.left, resolution.right))
    return manual, accepted, rejected


def _candidates(
    record: NormalizedRecord,
    bucket: Sequence[NormalizedRecord],
    tolerances: Tolerances,
    blocked: set[tuple[RecordKey, RecordKey]],
) -> list[tuple[Decimal, Decimal, str, NormalizedRecord]]:
    """Plausible counterparts for one record, ranked.

    Qualification is deliberately narrow - same instrument and side, quantity
    within tolerance, timestamp inside the suggestion window - and the window
    is much wider than the comparison tolerance, because a suggestion is a
    question and a comparison is an assertion (D5).
    """
    window = Decimal(tolerances.suggest_window_seconds)
    found: list[tuple[Decimal, Decimal, str, NormalizedRecord]] = []

    for other in bucket:
        if (record.key, other.key) in blocked:
            continue
        gap = abs(seconds_between(record.occurred_at, other.occurred_at))
        if gap > window:
            continue
        within_quantity, _, quantity_gap = compare_quantity(
            record.quantity, other.quantity, tolerances
        )
        if not within_quantity:
            continue
        found.append((gap, quantity_gap, other.reference, other))

    found.sort(key=lambda item: (item[0], item[1], item[2]))
    return found


def match(
    left: Sequence[NormalizedRecord],
    right: Sequence[NormalizedRecord],
    tolerances: Tolerances,
    prior_resolutions: Sequence[PriorResolution] = (),
) -> MatchResult:
    """Match two sides of a period. Same inputs, same output, every time."""
    manual, accepted, blocked = _resolution_index(prior_resolutions)

    # --- Tier 1: exclusion, before anything is matched (TR-305) -------------
    excluded_left = [r for r in left if r.is_cancelled]
    excluded_right = [r for r in right if r.is_cancelled]
    live_left = [r for r in left if not r.is_cancelled]
    live_right = [r for r in right if not r.is_cancelled]

    cancelled_left_refs = {r.reference for r in excluded_left}
    cancelled_right_refs = {r.reference for r in excluded_right}

    # A trade cancelled by one party and not the other is a real break, and
    # would be invisible if the cancelled row were merely set aside (D14).
    status_disagreements = [r for r in live_left if r.reference in cancelled_right_refs]
    status_disagreements += [r for r in live_right if r.reference in cancelled_left_refs]
    disagreeing = {(r.source_code, r.reference) for r in status_disagreements}

    available_left = {
        r.key: r for r in live_left if (r.source_code, r.reference) not in disagreeing
    }
    available_right = {
        r.key: r for r in live_right if (r.source_code, r.reference) not in disagreeing
    }

    pairs: list[MatchedPair] = []
    revocations: list[Revocation] = []

    # --- Tier 0: decisions a person already made, applied first (TR-304) ----
    for left_key, right_key in sorted(manual.items(), key=lambda kv: kv[0].reference):
        left_record = available_left.get(left_key)
        right_record = available_right.get(right_key)
        if left_record is None or right_record is None:
            # A resolution naming a record this run does not contain is inert.
            # It is not an error: the file may simply not have arrived yet.
            continue
        pairs.append(MatchedPair(left_record, right_record, PairOrigin.MANUAL))
        del available_left[left_key]
        del available_right[right_key]

    # --- Tier 2: reference equality (TR-307) --------------------------------
    right_by_reference = {r.reference: key for key, r in available_right.items()}
    for left_key in sorted(available_left, key=lambda k: k.reference):
        candidate_key = right_by_reference.get(left_key.reference)
        if candidate_key is None or candidate_key not in available_right:
            continue
        right_key = candidate_key
        left_record = available_left[left_key]
        right_record = available_right[right_key]
        pairs.append(MatchedPair(left_record, right_record, PairOrigin.REFERENCE))

        # The one automatic reversal in the system, and never a silent one.
        # An acceptance made on incomplete information does not survive the
        # counterpart turning up (R7.7, D10).
        for key in (left_key, right_key):
            if key in accepted:
                accepted.discard(key)
                revocations.append(
                    Revocation(
                        key=key,
                        reason=(
                            f"accepted as having no pair, but {right_record.reference} "
                            "now has a counterpart on the other side"
                        ),
                    )
                )
        del available_left[left_key]
        del available_right[right_key]

    # --- Tier 3: propose, never apply (TR-308, TR-309) ----------------------
    buckets: dict[tuple[str, str], list[NormalizedRecord]] = {}
    for record in available_right.values():
        buckets.setdefault(_bucket_key(record), []).append(record)
    for grouped in buckets.values():
        grouped.sort(key=_by_reference)

    suggestions: list[Suggestion] = []
    suggested_left: set[RecordKey] = set()
    suggested_right: set[RecordKey] = set()

    for left_key in sorted(available_left, key=lambda k: k.reference):
        record = available_left[left_key]
        if left_key in accepted:
            continue
        bucket = buckets.get(_bucket_key(record), [])
        found = _candidates(record, bucket, tolerances, blocked)
        for rank, (gap, quantity_gap, _, other) in enumerate(found, start=1):
            if other.key in accepted:
                continue
            suggestions.append(
                Suggestion(
                    left=record,
                    right=other,
                    rank=rank,
                    time_gap_seconds=gap,
                    qty_rel_diff=quantity_gap,
                    reason=(
                        f"same instrument and side, quantity within tolerance, "
                        f"{_minutes(gap)} apart"
                    ),
                )
            )
            suggested_left.add(left_key)
            suggested_right.add(other.key)

    # --- Tier 4: everything left, on both sides (TR-312) --------------------
    def remainder(
        pool: Iterable[NormalizedRecord], suggested: set[RecordKey]
    ) -> tuple[list[NormalizedRecord], list[NormalizedRecord]]:
        unmatched: list[NormalizedRecord] = []
        settled: list[NormalizedRecord] = []
        for record in pool:
            if record.key in accepted:
                settled.append(record)
            elif record.key in suggested:
                continue  # its state is "suggested"; it is not also unmatched
            else:
                unmatched.append(record)
        return unmatched, settled

    unmatched_left, accepted_left = remainder(available_left.values(), suggested_left)
    unmatched_right, accepted_right = remainder(available_right.values(), suggested_right)

    return MatchResult(
        pairs=tuple(sorted(pairs, key=lambda p: (p.left.reference, p.right.reference))),
        suggestions=tuple(
            sorted(suggestions, key=lambda s: (s.left.reference, s.rank, s.right.reference))
        ),
        unmatched_left=tuple(sorted(unmatched_left, key=_by_reference)),
        unmatched_right=tuple(sorted(unmatched_right, key=_by_reference)),
        accepted_unmatched=tuple(
            sorted([*accepted_left, *accepted_right], key=lambda r: (r.source_code, r.reference))
        ),
        status_disagreements=tuple(
            sorted(status_disagreements, key=lambda r: (r.source_code, r.reference))
        ),
        excluded_left=tuple(sorted(excluded_left, key=_by_reference)),
        excluded_right=tuple(sorted(excluded_right, key=_by_reference)),
        revocations=tuple(sorted(revocations, key=lambda r: (r.key.source_code, r.key.reference))),
    )
