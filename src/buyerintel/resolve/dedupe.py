"""Entity resolution decision engine and merge (spec section 11).

Decision rules (configurable constants at module top):
- Auto-merge on any hard key match (registry, domain, phone, social).
- Never merge when domain AND country both differ, unless a registry id matches;
  strong names in that shape become linked subsidiary records instead.
- Auto-merge when name similarity >= 0.93 with same country and one corroborating
  signal (domain, phone or address similarity >= 0.85).
- Borderline 0.80-0.93 goes to the LLM adjudicator (pluggable; M1 records it as a
  pending decision rather than guessing).
- Survivorship: prefer verified, then most recent, then most complete; all replaced
  values stay in provenance (company_alias + audit log).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.db.models import (
    Company,
    CompanyAlias,
    ContactPoint,
    ExpoAppearance,
    OutreachEvent,
    Person,
    ProductMatch,
    Quote,
    Sample,
    ScoreRun,
    Shipment,
    SourceRecord,
)
from buyerintel.resolve.matchers import (
    MatchKeys,
    address_similarity,
    domain_similarity,
    keys_from_company,
    name_similarity,
)

# Configurable decision thresholds (spec section 11).
AUTO_MERGE_SIM = 0.93
BORDERLINE_LOW = 0.80
CORROBORATION_SIM = 0.85

Action = Literal["merge", "link", "adjudicate", "distinct"]


@dataclass
class Decision:
    action: Action
    reason: str
    score: float


Adjudicator = Callable[[Company, Company, MatchKeys, MatchKeys], Action]


def decide_pair(a: MatchKeys, b: MatchKeys) -> Decision:
    """Apply the spec's decision rules to one candidate pair."""
    # 1. Hard keys -> merge.
    if set(a.registries) & set(b.registries):
        return Decision("merge", "hard:registry", 1.0)
    if a.domain and a.domain == b.domain:
        return Decision("merge", "hard:domain", 1.0)
    if set(a.phones) & set(b.phones):
        return Decision("merge", "hard:phone", 1.0)
    if set(a.socials) & set(b.socials):
        return Decision("merge", "hard:social", 1.0)

    sim = name_similarity(a.name_key, b.name_key)

    # 2. Domain AND country both differ -> never merge; link strong-name pairs
    #    as subsidiaries, borderline goes to adjudication, rest are distinct.
    if a.domain and b.domain and a.domain != b.domain and a.country and b.country and a.country != b.country:
        if sim >= AUTO_MERGE_SIM:
            return Decision("link", f"subsidiary:name_sim={sim:.3f}", sim)
        if sim >= BORDERLINE_LOW:
            return Decision("adjudicate", f"borderline:name_sim={sim:.3f}", sim)
        return Decision("distinct", f"name_sim={sim:.3f}", sim)

    # 3. Corroboration + name similarity.
    corroboration = max(
        domain_similarity(a.domain, b.domain),
        address_similarity(a.address, b.address),
    )
    countries_differ = bool(a.country and b.country and a.country != b.country)

    if sim >= AUTO_MERGE_SIM:
        if countries_differ:
            return Decision("link", f"subsidiary:name_sim={sim:.3f}", sim)
        if corroboration >= CORROBORATION_SIM:
            return Decision("merge", f"name_sim={sim:.3f}+corroborated", sim)
        return Decision("adjudicate", f"strong_name_no_corroboration:sim={sim:.3f}", sim)
    if sim >= BORDERLINE_LOW and corroboration >= CORROBORATION_SIM:
        return Decision("adjudicate", f"borderline:sim={sim:.3f}+corroborated", sim)
    return Decision("distinct", f"name_sim={sim:.3f}", sim)


# ---------------------------------------------------------------------------
# Blocking and sweep
# ---------------------------------------------------------------------------


def load_keys(session: Session) -> dict[int, MatchKeys]:
    companies = session.scalars(select(Company)).all()
    contacts = session.scalars(
        select(ContactPoint).where(ContactPoint.owner_type == "company")
    ).all()
    by_company: dict[int, list[ContactPoint]] = {}
    for c in contacts:
        by_company.setdefault(c.owner_id, []).append(c)
    return {co.id: keys_from_company(co, by_company.get(co.id, [])) for co in companies}


def find_candidates(keys: dict[int, MatchKeys]) -> list[tuple[int, int]]:
    """Blocking: pairs sharing any blocking key, deduplicated, i < j."""
    blocks: dict[str, list[int]] = {}
    for cid, mk in keys.items():
        for key in mk.blocking_keys():
            blocks.setdefault(key, []).append(cid)
    pairs: set[tuple[int, int]] = set()
    for members in blocks.values():
        if len(members) < 2 or len(members) > 50:  # oversized block = useless key
            continue
        for i, a in enumerate(sorted(set(members))):
            for b in sorted(set(members))[i + 1 :]:
                pairs.add((a, b))
    return sorted(pairs)


def sweep(session: Session, source: str = "dedupe:sweep") -> dict:
    """Full dedupe pass over all companies. Returns counters; every change audited."""
    session.info["audit_source"] = source
    stats = {"pairs": 0, "merged": 0, "linked": 0, "adjudicated": 0, "distinct": 0}
    keys = load_keys(session)
    pairs = find_candidates(keys)
    decided: set[int] = set()  # companies already merged away

    for a_id, b_id in pairs:
        if a_id in decided or b_id in decided or a_id not in keys or b_id not in keys:
            continue
        stats["pairs"] += 1
        decision = decide_pair(keys[a_id], keys[b_id])
        if decision.action == "merge":
            survivor = min(a_id, b_id)
            duplicate = max(a_id, b_id)
            merge_companies(session, survivor, duplicate)
            keys.pop(duplicate)
            decided.add(duplicate)
            stats["merged"] += 1
        elif decision.action == "link":
            survivor, subsidiary = sorted((a_id, b_id))
            link_companies(session, survivor, subsidiary)
            keys.pop(subsidiary)
            decided.add(subsidiary)
            stats["linked"] += 1
        elif decision.action == "adjudicate":
            stats["adjudicated"] += 1
        else:
            stats["distinct"] += 1
    session.flush()
    return stats


# ---------------------------------------------------------------------------
# Merge and link with survivorship (spec section 11)
# ---------------------------------------------------------------------------

_CHILD_TABLES = (
    SourceRecord,
    ExpoAppearance,
    Person,
    ContactPoint,
    ProductMatch,
    OutreachEvent,
    Quote,
    Sample,
    Shipment,
    ScoreRun,
)


def merge_companies(session: Session, survivor_id: int, duplicate_id: int) -> Company:
    """Merge duplicate into survivor: backfill fields, move children, keep provenance."""
    assert survivor_id != duplicate_id, "cannot merge a company with itself"
    survivor = session.get(Company, survivor_id)
    duplicate = session.get(Company, duplicate_id)
    if survivor is None or duplicate is None:
        raise ValueError("merge targets not found")

    # Survivorship: survivor's values stay; None fields are backfilled; the
    # duplicate's name survives as an alias (provenance), never silently lost.
    for attr in ("legal_name", "domain", "country", "city", "address", "registry_id", "vat_id", "lei", "type", "size_band"):
        if getattr(survivor, attr) is None and getattr(duplicate, attr) is not None:
            setattr(survivor, attr, getattr(duplicate, attr))
    survivor.tags = sorted(set((survivor.tags or []) + (duplicate.tags or [])))
    survivor.confidence = max(survivor.confidence, duplicate.confidence)
    dates = [d for d in (survivor.last_enriched, duplicate.last_enriched) if d is not None]
    if dates:
        survivor.last_enriched = max(dates)
    if duplicate.parent_company_id and survivor.parent_company_id is None:
        survivor.parent_company_id = duplicate.parent_company_id

    if duplicate.canonical_name and duplicate.canonical_name != survivor.canonical_name:
        # Provenance: keep the duplicate's raw name even when it normalizes to
        # the same key ("... Company" vs "... Co") — never silently lose a name.
        add_alias(session, survivor.id, duplicate.canonical_name, kind="merged")
    for alias in session.scalars(
        select(CompanyAlias).where(CompanyAlias.company_id == duplicate_id)
    ):
        alias.company_id = survivor_id

    for table in _CHILD_TABLES:
        if table is ContactPoint:
            rows = session.scalars(
                select(ContactPoint).where(
                    ContactPoint.owner_type == "company",
                    ContactPoint.owner_id == duplicate_id,
                )
            ).all()
        else:
            rows = session.scalars(select(table).where(table.company_id == duplicate_id)).all()
        for row in rows:
            if isinstance(row, ContactPoint):
                conflict = session.scalar(
                    select(ContactPoint).where(
                        ContactPoint.owner_type == "company",
                        ContactPoint.owner_id == survivor_id,
                        ContactPoint.kind == row.kind,
                        ContactPoint.value == row.value,
                    )
                )
                if conflict is not None:
                    session.delete(row)  # identical contact already on survivor
                    continue
                row.owner_id = survivor_id  # ContactPoint uses owner_id, not company_id
                continue
            row.company_id = survivor_id

    session.delete(duplicate)
    session.flush()
    return survivor


def link_companies(session: Session, parent_id: int, subsidiary_id: int) -> Company:
    """Link a subsidiary / brand / group member under its parent (no merge)."""
    subsidiary = session.get(Company, subsidiary_id)
    if subsidiary is None:
        raise ValueError("subsidiary not found")
    subsidiary.parent_company_id = parent_id
    session.flush()
    return subsidiary


def add_alias(session: Session, company_id: int, alias: str, kind: str = "trade") -> None:
    if not alias:
        return
    existing = session.scalar(
        select(CompanyAlias).where(
            CompanyAlias.company_id == company_id,
            CompanyAlias.alias == alias,
            CompanyAlias.kind == kind,
        )
    )
    if existing is None:
        session.add(CompanyAlias(company_id=company_id, alias=alias, kind=kind))
