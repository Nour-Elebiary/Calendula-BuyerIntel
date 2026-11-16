"""Weighted scoring, tier assignment, ScoreRun persistence (spec section 13).

Weights come from config/weights.yaml **v2** (personas doc section 9), which
supersedes the spec's v1 7-component table per the additions precedence:

  persona_fit_product_overlap 35 | scale_buying_frequency 25 |
  certification_alignment 15     | reachability 15            | timing_signals 10

Every component returns (points, detail) so the breakdown stored in
``score_run.components`` is fully reproducible. Tiers (weights.yaml):

  Tier 1: total >= 70 AND persona_fit_product_overlap >= 14
  Tier 2: 45-69 | Tier 3: < 45

The spec-fit flag (Ph. Eur./USP/BP/Codex/cut size) is INFORMATIONAL ONLY -
it never moves the score (docs/02 section 2).
"""
from __future__ import annotations

import statistics
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.config import load_yaml
from buyerintel.db.models import (
    Company,
    ContactPoint,
    EnrichCheckpoint,
    ExpoAppearance,
    ExpoEdition,
    Person,
    ProductMatch,
    ScoreRun,
)
from buyerintel.score.personas import classify

# Certifications Calendula holds; alignment counts how many the buyer's
# profile references (tags from enrichment + profile text).
CERT_TOKENS = {
    "organic": ["organic", "bio", "nop", "eu organic"],
    "halal": ["halal"],
    "kosher": ["kosher"],
    "iso": ["iso 9001", "iso 22000", "iso"],
    "brcgs": ["brcgs", "brc"],
    "fda": ["fda"],
    "gmp": ["gmp"],
}

SPEC_FIT_STANDARDS = {
    "ph_eur": ["ph. eur", "ph.eur", "european pharmacopoeia"],
    "usp": ["usp", "united states pharmacopoeia"],
    "bp": ["british pharmacopoeia", "bp monograph"],
    "codex": ["codex"],
    "cut_size": ["cut size", "granulation", "mesh size", "tea bag cut"],
}


def spec_fit_flag(company: Company, extra_text: str = "") -> list[str]:
    """Standards the buyer's profile mentions. Informational; no score effect."""
    text = " ".join([
        company.canonical_name or "", " ".join(company.tags or []),
        company.type or "", extra_text,
    ]).lower()
    found = []
    for standard, needles in SPEC_FIT_STANDARDS.items():
        if any(needle in text for needle in needles):
            found.append(standard)
    return found


def _cap(value: float, maximum: float) -> float:
    return round(min(maximum, max(0.0, value)), 2)


# ------------------------------------------------------------------ components
def component_persona_product(session: Session, company: Company, max_points: float) -> tuple[float, dict]:
    """0-35: persona fit (0-15) + product overlap with the 42 SKUs (0-20)."""
    persona, sim, hits = classify(session, company)
    persona_points = _cap(15.0 * (sim or 0.0), 15.0)

    matches = session.scalars(
        select(ProductMatch).where(ProductMatch.company_id == company.id)
    ).all()
    best_per_product: dict[int, float] = {}
    for m in matches:
        best_per_product[m.product_id] = max(best_per_product.get(m.product_id, 0.0), m.score)
    total_match = sum(best_per_product.values())
    # 2-3 strong product hits saturate the overlap part.
    overlap_points = _cap(20.0 * min(1.0, total_match / 2.0), 20.0)
    return _cap(persona_points + overlap_points, max_points), {
        "persona": persona.persona_id if persona else None,
        "persona_similarity": sim,
        "persona_keyword_hits": hits,
        "persona_points": persona_points,
        "matched_products": len(best_per_product),
        "match_score_sum": round(total_match, 3),
        "overlap_points": overlap_points,
    }


def component_scale_frequency(session: Session, company: Company, persona: dict,
                              max_points: float) -> tuple[float, dict]:
    """0-25: order-size intent + size band + expo presence + shipment frequency.

    M7 wire-in (logged in DECISIONS.md): repeat-order frequency and volume
    from the shipment table join the proxies. Sub-caps: order_size 8,
    size_band 4, expo 5, shipments 8 (frequency 0-5 + volume-vs-MOQ 0-3).
    """
    d = persona.get("_definition") or {}
    order_kg = float(d.get("typical_order_kg") or 0)
    size_points = 8.0 if order_kg >= 1000 else (5.0 if order_kg >= 500 else 2.0) if order_kg else 0.0

    band = (company.size_band or "").lower()
    band_points = {"large": 4.0, "medium": 2.5, "small": 1.0}.get(band, 0.0)

    appearances = session.scalars(
        select(ExpoAppearance).where(ExpoAppearance.company_id == company.id)
    ).all()
    editions = len({a.edition_id for a in appearances})
    expo_points = _cap(1.5 * len(appearances) + (2.0 if editions > 1 else 0.0), 5.0)

    from buyerintel.db.models import Shipment

    shipments = session.scalars(
        select(Shipment).where(Shipment.company_id == company.id)
    ).all()
    n = len(shipments)
    freq_points = 0.0
    if n == 1:
        freq_points = 2.0
    elif 2 <= n <= 3:
        freq_points = 4.0
    elif n >= 4:
        freq_points = 5.0
    # shipment volume against the persona's typical order (MOQ proxy)
    quantities = [s.qty for s in shipments if s.qty]
    volume_points = 0.0
    if quantities and order_kg:
        median_qty = statistics.median(quantities)
        volume_points = 3.0 if median_qty >= order_kg else \
            (2.0 if median_qty >= order_kg / 2 else 1.0)
    elif quantities:
        volume_points = 1.0  # some real volume, no persona MOQ to compare
    shipment_points = _cap(freq_points + volume_points, 8.0)

    return _cap(size_points + band_points + expo_points + shipment_points, max_points), {
        "persona_typical_order_kg": order_kg, "order_size_points": size_points,
        "size_band": company.size_band, "size_band_points": band_points,
        "expo_appearances": len(appearances), "distinct_editions": editions,
        "expo_points": expo_points,
        "shipments": n, "shipment_freq_points": freq_points,
        "median_shipment_qty": statistics.median(quantities) if quantities else None,
        "shipment_volume_points": volume_points, "shipment_points": shipment_points,
    }


def component_certification(session: Session, company: Company, extra_text: str,
                            max_points: float) -> tuple[float, dict]:
    """0-15: how many of Calendula's certifications the buyer's world references."""
    text = " ".join([
        company.canonical_name or "", company.type or "",
        " ".join(company.tags or []), extra_text,
    ]).lower()
    found = [cert for cert, needles in CERT_TOKENS.items() if any(n in text for n in needles)]
    points = _cap(2.5 * len(found), max_points)
    return points, {"certs_found": found, "points_per_cert": 2.5}


def component_reachability(session: Session, company: Company, max_points: float) -> tuple[float, dict]:
    """0-15: decision maker 5, verified/published email 5, phone 2.5, social 2.5."""
    dm = session.scalar(
        select(Person).where(Person.company_id == company.id, Person.is_decision_maker.is_(True))
    )
    dm_points = 5.0 if dm else 0.0

    contacts = session.scalars(
        select(ContactPoint).where(ContactPoint.owner_type == "company",
                                   ContactPoint.owner_id == company.id)
    ).all()
    email_points = 0.0
    for c in contacts:
        if c.kind != "email":
            continue
        if c.verified_status in ("verified", "published"):
            email_points = max(email_points, 5.0 if c.verified_status == "verified" else 4.0)
    phone_points = 2.5 if any(c.kind == "phone" for c in contacts) else 0.0
    social_points = 2.5 if any(c.kind in ("linkedin", "facebook", "instagram", "tiktok", "x", "youtube")
                               for c in contacts) else 0.0
    return _cap(dm_points + email_points + phone_points + social_points, max_points), {
        "decision_maker": dm.full_name if dm else None, "decision_maker_points": dm_points,
        "email_points": email_points, "phone_points": phone_points,
        "social_points": social_points,
        "contacts_total": len(contacts),
    }


def component_timing(session: Session, company: Company, max_points: float) -> tuple[float, dict]:
    """0-10: recent expo edition 3, recent site activity (archive) 2,
    multi-edition recency 2, import-record recency 3 (M7 wire-in).

    Latest shipment within `scoring.shipment_recency_days` (default 180)
    scores 3; within double that, 2.
    """
    now = datetime.now(timezone.utc)
    editions = session.execute(
        select(ExpoEdition.year).join(ExpoAppearance, ExpoAppearance.edition_id == ExpoEdition.id)
        .where(ExpoAppearance.company_id == company.id)
    ).scalars().all()
    recent_expo = [y for y in editions if y and (now.year - y) <= 2]
    expo_points = 3.0 if recent_expo else 0.0

    from buyerintel.db.models import ArchiveSnapshot, Shipment

    snapshots = session.scalars(
        select(ArchiveSnapshot.timestamp).where(ArchiveSnapshot.url.like(f"%{company.domain or '@none@'}%"))
    ).all()
    def _aware(ts: datetime) -> datetime:
        # SQLite returns naive datetimes; normalize for the subtraction
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

    recent_archive = any(ts and (now - _aware(ts)).days <= 730 for ts in snapshots)
    archive_points = 2.0 if recent_archive else 0.0
    multi_points = 2.0 if len(set(recent_expo)) > 1 else 0.0

    recency_days = float((load_yaml("shipments.yaml").get("scoring") or {})
                         .get("shipment_recency_days", 180))
    latest = session.scalar(
        select(Shipment.date).where(Shipment.company_id == company.id,
                                    Shipment.date.is_not(None))
        .order_by(Shipment.date.desc()).limit(1)
    )
    shipment_points = 0.0
    shipment_recency = None
    if latest is not None:
        shipment_recency = (now - _aware(latest)).days
        if shipment_recency <= recency_days:
            shipment_points = 3.0
        elif shipment_recency <= recency_days * 2:
            shipment_points = 2.0

    return _cap(expo_points + archive_points + multi_points + shipment_points, max_points), {
        "recent_expo_years": sorted(set(recent_expo), reverse=True), "expo_points": expo_points,
        "recent_site_activity": recent_archive, "archive_points": archive_points,
        "multi_edition_points": multi_points,
        "latest_shipment_days_ago": shipment_recency, "shipment_recency_points": shipment_points,
    }


# ------------------------------------------------------------------ assembly
def load_weights(config: dict | None = None) -> tuple[int, dict, dict]:
    """(version, weights, tier_thresholds) from config/weights.yaml."""
    data = config or load_yaml("weights.yaml")
    return int(data.get("version", 1)), dict(data["weights"]), dict(data.get("tier_thresholds", {}))


def assign_tier(total: float, components: dict, thresholds: dict) -> int:
    t1 = thresholds.get("tier1", {})
    t2 = thresholds.get("tier2", {})
    if total >= float(t1.get("min_total", 70)) and \
            components.get("persona_fit_product_overlap", 0) >= float(t1.get("min_product_fit", 14)):
        return 1
    if total >= float(t2.get("min_total", 45)):
        return 2
    return 3


def score_company(session: Session, company: Company, *, weights: dict | None = None,
                  thresholds: dict | None = None, version: int | None = None,
                  extra_text: str = "") -> ScoreRun:
    """Score one company and persist a reproducible ScoreRun row.

    Tier changes are written to company.tier - the audit layer logs them
    (M5 acceptance: "tier changes logged").
    """
    w_version, w, t = load_weights()
    if weights is not None:
        w = weights
    if thresholds is not None:
        t = thresholds
    version = version if version is not None else w_version

    persona, _sim, _hits = classify(session, company)
    persona_def = persona.definition if persona else {}

    persona_component, persona_detail = component_persona_product(session, company, w["persona_fit_product_overlap"])
    persona_detail["_definition"] = persona_def  # for the scale component only
    scale_component, scale_detail = component_scale_frequency(
        session, company, persona_detail, w["scale_buying_frequency"])
    persona_detail.pop("_definition")
    cert_component, cert_detail = component_certification(session, company, extra_text,
                                                          w["certification_alignment"])
    reach_component, reach_detail = component_reachability(session, company, w["reachability"])
    timing_component, timing_detail = component_timing(session, company, w["timing_signals"])

    components = {
        "persona_fit_product_overlap": persona_component,
        "scale_buying_frequency": scale_component,
        "certification_alignment": cert_component,
        "reachability": reach_component,
        "timing_signals": timing_component,
    }
    details = {
        "persona_fit_product_overlap": persona_detail,
        "scale_buying_frequency": scale_detail,
        "certification_alignment": cert_detail,
        "reachability": reach_detail,
        "timing_signals": timing_detail,
    }
    total = _cap(sum(components.values()), 100.0)
    tier = assign_tier(total, components, t)

    run = ScoreRun(company_id=company.id, components={"components": components, "detail": details},
                   total=total, tier=tier, weights_version=version)
    session.add(run)
    old_tier = company.tier
    company.tier = tier
    company.score = total
    session.flush()
    run.components = {"components": components, "detail": details, "previous_tier": old_tier,
                      "spec_fit": spec_fit_flag(company, extra_text)}
    session.flush()
    return run


def pending_companies(session: Session, *, limit: int | None = None,
                      tiers: list[int] | None = None) -> list[Company]:
    """Companies that have completed enrichment (s2+) but were never scored."""
    done_ids = {row.company_id for row in session.scalars(
        select(ScoreRun.company_id)).all()}
    query = select(Company).outerjoin(
        EnrichCheckpoint, EnrichCheckpoint.company_id == Company.id
    ).where(
        Company.id.not_in(done_ids) if done_ids else Company.id.is_not(None)
    ).where(
        (EnrichCheckpoint.stage.in_(["s2", "s3", "s4"])) | (EnrichCheckpoint.id.is_(None))
    ).order_by(Company.id)
    if tiers:
        query = query.where(Company.tier.in_(tiers))
    companies = list(session.scalars(query).unique().all())
    return companies[:limit] if limit else companies
