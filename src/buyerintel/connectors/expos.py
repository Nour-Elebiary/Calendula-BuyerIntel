"""Expo ingestion (spec section 11).

One YAML mapping per expo under ``config/expo_maps/`` defines column aliases,
languages and cleanups. Ingestion is idempotent via ``raw_hash``; each row creates
a ``source_record`` and an ``expo_appearance``; companies appearing in several
editions stay distinct companies with multiple appearance records (activity signal).
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.config import CONFIG_DIR
from buyerintel.connectors.importer import import_workbook
from buyerintel.db.models import ExpoAppearance, SourceRecord


def resolve_map(name: str) -> Path:
    """Resolve an expo map by name or explicit path."""
    candidate = Path(name)
    if candidate.suffix in {".yaml", ".yml"} and candidate.exists():
        return candidate
    by_name = CONFIG_DIR / "expo_maps" / f"{name}.yaml"
    if by_name.exists():
        return by_name
    raise FileNotFoundError(f"expo map not found: {name} (looked in {CONFIG_DIR / 'expo_maps'})")


def ingest_expo(
    session: Session,
    file_path: str | Path,
    map_name: str,
    *,
    expo_year: int | None = None,
) -> dict:
    """Ingest one exhibitor list; re-running is a no-op (M1 acceptance)."""
    map_path = resolve_map(map_name)
    import yaml

    mapping = yaml.safe_load(map_path.read_text(encoding="utf-8")) or {}
    expo_name = mapping.get("expo") or map_name
    year = expo_year or mapping.get("year")
    stats = import_workbook(session, file_path, map_path, expo_name=expo_name, expo_year=year)
    appearances = create_appearances(session)
    return {**stats.__dict__, "appearances_created": appearances}


def create_appearances(session: Session) -> int:
    """Create one expo_appearance per (company, edition); deduplicated by constraint."""
    created = 0
    records = session.scalars(
        select(SourceRecord).where(SourceRecord.expo_edition_id.is_not(None))
    ).all()
    seen: set[tuple[int, int]] = set()
    for record in records:
        if record.company_id is None:
            continue
        pair = (record.company_id, record.expo_edition_id)
        if pair in seen:
            continue
        seen.add(pair)
        existing = session.scalar(
            select(ExpoAppearance).where(
                ExpoAppearance.company_id == record.company_id,
                ExpoAppearance.edition_id == record.expo_edition_id,
            )
        )
        if existing is None:
            session.add(
                ExpoAppearance(
                    company_id=record.company_id,
                    edition_id=record.expo_edition_id,
                    listed_products=(record.raw_json or {}).get("products"),
                )
            )
            created += 1
    session.flush()
    return created
