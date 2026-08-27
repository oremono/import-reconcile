"""Seed the sources and tolerance profiles a run needs.

Idempotent: running it twice changes nothing and says so. It deliberately does
NOT ingest the sample CSVs - uploading those is the first thing the analyst
does, and watching it happen is the point of the demo.

Usage:  uv run python -m app.seed
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Source, ToleranceProfile
from app.db.session import get_session
from app.sources import DEFAULT_TOLERANCES, SOURCE_NAMES, SOURCE_PAIRS, SOURCES
from core.format import source_format_from_config
from core.tolerance import tolerances_from_config


def seed_sources(session: Session) -> tuple[list[str], list[str]]:
    """Insert any missing source rows. Returns (created, unchanged) codes."""
    created: list[str] = []
    unchanged: list[str] = []
    for code, config in SOURCES.items():
        # Validate before storing: a configuration that cannot load rows should
        # fail here, not on the morning someone uploads a file against it.
        source_format_from_config(code, config)
        if session.scalar(select(Source).where(Source.code == code)) is not None:
            unchanged.append(code)
            continue
        session.add(Source(code=code, name=SOURCE_NAMES[code], format_config=config))
        created.append(code)
    session.flush()
    return created, unchanged


def seed_tolerance_profiles(session: Session) -> tuple[list[str], list[str]]:
    """Insert any missing tolerance profile. Returns (created, unchanged) labels."""
    tolerances = tolerances_from_config(DEFAULT_TOLERANCES)
    created: list[str] = []
    unchanged: list[str] = []
    for left_code, right_code in SOURCE_PAIRS:
        left = session.scalar(select(Source).where(Source.code == left_code))
        right = session.scalar(select(Source).where(Source.code == right_code))
        if left is None or right is None:
            raise RuntimeError(f"cannot profile {left_code} against {right_code}: source missing")
        label = f"{left_code} <-> {right_code}"
        existing = session.scalar(
            select(ToleranceProfile).where(
                ToleranceProfile.left_source_id == left.id,
                ToleranceProfile.right_source_id == right.id,
            )
        )
        if existing is not None:
            unchanged.append(label)
            continue
        session.add(
            ToleranceProfile(
                left_source_id=left.id,
                right_source_id=right.id,
                amount_bps=tolerances.amount_bps,
                amount_abs_floor=tolerances.amount_abs_floor,
                price_bps=tolerances.price_bps,
                qty_bps=tolerances.qty_bps,
                time_tolerance_seconds=tolerances.time_tolerance_seconds,
                suggest_window_seconds=tolerances.suggest_window_seconds,
            )
        )
        created.append(label)
    session.flush()
    return created, unchanged


def seed(session: Session) -> None:
    made_sources, kept_sources = seed_sources(session)
    made_profiles, kept_profiles = seed_tolerance_profiles(session)

    for label, made, kept in (
        ("sources", made_sources, kept_sources),
        ("tolerance profiles", made_profiles, kept_profiles),
    ):
        if made:
            print(f"  created {label}: {', '.join(made)}")
        if kept:
            print(f"  already present {label}: {', '.join(kept)}")


def main() -> None:
    print("Seeding reference data")
    with get_session() as session:
        seed(session)
    print("Done. Upload a file from the home page to start a run.")


if __name__ == "__main__":
    main()
