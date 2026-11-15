"""Rule chain: ordered state machine (spec section 14).

Each rule is a small, individually testable function taking
(session, company, ctx) and returning a RuleResult:

  verdict "pass"  -> the chain continues
  verdict "stop"  -> the chain stops; company.state/state_reason are written

Ordered rules (spec section 14):
  1. dedupe_check          2. liveness        3. competitor_check
  4. persona_match         5. product_fit     6. scoring_and_tier
  7. enrichment_depth      8. contact_confidence
  9. final_state           (writes `active` when everything passed)

States: active | needs_enrichment | parked(reason) | excluded(reason).
Tier assignment treats all countries alike (including China).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.config import load_yaml
from buyerintel.db.models import Company, ContactPoint, EnrichCheckpoint
from buyerintel.score import scoring
from buyerintel.score import personas as personas_mod

COMPETITOR_TYPES = ["exporter", "supplier", "grower", "producer", "farm", "cooperative"]


@dataclass
class RuleResult:
    name: str
    verdict: str                    # pass | stop
    state: str | None = None        # active | needs_enrichment | parked | excluded
    reason: str | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class RuleContext:
    """Everything the rules may need; built once per run."""
    config: dict
    weights: dict
    thresholds: dict
    weights_version: int
    llm: Any = None                 # optional LLM router (product-match borderline)
    extra_text: str = ""            # profile text captured during enrichment


def load_context(llm: Any = None, config: dict | None = None) -> RuleContext:
    cfg = config or load_yaml("score.yaml")
    version, weights, thresholds = scoring.load_weights()
    return RuleContext(config=cfg, weights=weights, thresholds=thresholds,
                       weights_version=version, llm=llm)


# ------------------------------------------------------------------ 1. dedupe
def dedupe_check(session: Session, company: Company, ctx: RuleContext) -> RuleResult:
    """Another EARLIER company with the same domain or normalized name =
    unresolved duplicate. The earliest company (lowest id) survives; later
    duplicates park pointing at it."""
    normalized = company.canonical_name.strip().lower()
    others = session.scalars(
        select(Company).where(Company.id != company.id)
    ).all()
    for other in sorted(others, key=lambda c: c.id):
        if other.id > company.id:
            continue  # a later duplicate parks against THIS company, not vice versa
        same_domain = (company.domain and other.domain
                       and company.domain.lower() == other.domain.lower())
        same_name = other.canonical_name.strip().lower() == normalized
        if same_domain or same_name:
            return RuleResult(
                "dedupe", "stop", "parked", f"unresolved duplicate of company {other.id}",
                {"duplicate_of": other.id, "match_on": "domain" if same_domain else "name"})
    return RuleResult("dedupe", "pass")


# ---------------------------------------------------------------- 2. liveness
def liveness(session: Session, company: Company, ctx: RuleContext) -> RuleResult:
    """Website responds, registry active, or recent social activity.

    A domain whose crawl fetched nothing and with no socials = defunct ->
    excluded(defunct). No domain at all and no socials = parked(no_website)
    (it may still be a real trader reachable off-web).
    """
    min_pages = int(ctx.config["rules"]["liveness_min_pages"])

    checkpoint = session.scalar(
        select(EnrichCheckpoint).where(EnrichCheckpoint.company_id == company.id)
    )
    crawled = bool(checkpoint and checkpoint.detail and checkpoint.detail.get("pages", 0) >= min_pages)
    socials = session.scalar(
        select(ContactPoint).where(
            ContactPoint.owner_type == "company", ContactPoint.owner_id == company.id,
            ContactPoint.kind.in_(["linkedin", "facebook", "instagram"]))
    )
    if crawled or socials or company.registry_id:
        return RuleResult("liveness", "pass", detail={"crawled": crawled, "socials": bool(socials)})
    if not company.domain:
        return RuleResult("liveness", "stop", "parked", "no_website",
                          {"note": "no domain, no socials, no registry id"})
    return RuleResult("liveness", "stop", "excluded", "defunct",
                      {"note": f"domain {company.domain} unreachable, no alternative signal"})


# -------------------------------------------------------------- 3. competitor
def competitor_check(session: Session, company: Company, ctx: RuleContext) -> RuleResult:
    """Exporters/suppliers of the same products -> excluded(competitor),
    kept in the competitor-intel list (tagged)."""
    haystack = " ".join([
        company.canonical_name or "", company.type or "",
        " ".join(company.tags or []), ctx.extra_text,
    ]).lower()
    # Explicit "we buy / we import" beats a generic type tag: an importer that
    # is *also* tagged exporter stays a buyer (many importers describe
    # themselves as "spice exporter" from their own origin).
    buyer_markers = ["import", "buyer", "purchasing", "procurement", "distributor",
                     "wholesaler", "trading"]
    is_buyer = any(m in haystack for m in buyer_markers)
    for keyword in ctx.config["rules"]["competitor_keywords"]:
        if keyword in haystack and not is_buyer:
            tags = list(company.tags or [])
            if "competitor-intel" not in tags:
                tags.append("competitor-intel")
                company.tags = tags
            return RuleResult("competitor", "stop", "excluded", "competitor",
                              {"keyword": keyword, "kept_in": "competitor-intel list"})
    return RuleResult("competitor", "pass")


# ------------------------------------------------------------ 4. persona
def persona_match(session: Session, company: Company, ctx: RuleContext) -> RuleResult:
    """Weak/no persona -> parked(persona_unclear); the match itself persists
    via CompanyPersona rows."""
    best, sim = personas_mod.store_company_personas(session, company)
    if best is None or sim < float(ctx.config["rules"]["persona_min_similarity"]):
        return RuleResult("persona", "stop", "parked", "persona_unclear",
                          {"best_similarity": sim})
    return RuleResult("persona", "pass", detail={"persona": best.persona_id,
                                                 "similarity": sim})


# ---------------------------------------------------------- 5. product fit
def product_fit(session: Session, company: Company, ctx: RuleContext) -> RuleResult:
    """No product overlap with the 42 SKUs -> parked(no_product_fit)."""
    from buyerintel.score.matching import match_products

    result = match_products(session, company, llm=ctx.llm, config=ctx.config)
    if not result.matches:
        return RuleResult("product_fit", "stop", "parked", "no_product_fit",
                          {"borderline": len(result.borderline)})
    return RuleResult("product_fit", "pass", detail={"matched": len(result.matches)})


# ------------------------------------------------------ 6. scoring and tier
def scoring_and_tier(session: Session, company: Company, ctx: RuleContext) -> RuleResult:
    """Run the full weighted score; assign tier (all countries alike)."""
    run = scoring.score_company(session, company, weights=ctx.weights,
                                thresholds=ctx.thresholds,
                                version=ctx.weights_version, extra_text=ctx.extra_text)
    return RuleResult("scoring", "pass", detail={
        "total": run.total, "tier": run.tier, "weights_version": run.weights_version,
        "components": run.components.get("components") if run.components else {},
        "previous_tier": (run.components or {}).get("previous_tier"),
    })


# ------------------------------------------------- 7. enrichment depth / gate
def enrichment_depth(session: Session, company: Company, ctx: RuleContext) -> RuleResult:
    """Tier gates enrichment depth. The paid-tool gate (S7, M6) fires for
    Tier 1 only; until M6 it records the decision without spending credits."""
    tier = company.tier or 3
    paid_tools_allowed = tier == 1
    return RuleResult("enrichment_depth", "pass", detail={
        "tier": tier, "max_stage": "s4", "paid_tool_gate": {
            "allowed": paid_tools_allowed,
            "status": "armed_for_m6" if paid_tools_allowed else "never_fires",
        }})


# --------------------------------------------------- 8. contact confidence
def contact_confidence(session: Session, company: Company, ctx: RuleContext) -> RuleResult:
    """Best contact below the confidence floor -> needs_enrichment (auto-retry)."""
    floor = float(ctx.config["rules"]["contact_confidence_floor"])
    contacts = session.scalars(
        select(ContactPoint).where(ContactPoint.owner_type == "company",
                                   ContactPoint.owner_id == company.id)
    ).all()
    # people's contacts count too
    from buyerintel.db.models import Person

    person_ids = session.scalars(
        select(Person.id).where(Person.company_id == company.id)).all()
    if person_ids:
        contacts.extend(session.scalars(
            select(ContactPoint).where(ContactPoint.owner_type == "person",
                                       ContactPoint.owner_id.in_(person_ids))
        ).all())
    best = max((c.confidence for c in contacts), default=0.0)
    usable = [c for c in contacts if c.confidence >= floor]
    if not usable:
        return RuleResult("contact_confidence", "stop", "needs_enrichment",
                          "low_contact_confidence",
                          {"best_confidence": best, "floor": floor, "contacts": len(contacts)})
    return RuleResult("contact_confidence", "pass", detail={
        "best_confidence": round(best, 3), "usable_contacts": len(usable)})


# --------------------------------------------------------- 9. final state
def final_state(session: Session, company: Company, ctx: RuleContext) -> RuleResult:
    """Everything passed: active."""
    return RuleResult("final_state", "pass", "active", "all_rules_passed",
                      {"spec_fit": scoring.spec_fit_flag(company, ctx.extra_text)})


# The ordered chain. New rules from 03_ADDITIONS slot in here.
RULES: list[Callable[[Session, Company, RuleContext], RuleResult]] = [
    dedupe_check,
    liveness,
    competitor_check,
    persona_match,
    product_fit,
    scoring_and_tier,
    enrichment_depth,
    contact_confidence,
    final_state,
]


def run_rule_chain(session: Session, company: Company, ctx: RuleContext | None = None,
                   *, rules: list | None = None) -> RuleResult:
    """Run the ordered chain; write the final state + reason to the company.

    Returns the LAST rule result evaluated (the stopping one, or final_state).
    company.state / state_reason / tier / score changes flow through the
    audit layer automatically.
    """
    ctx = ctx or load_context()
    chain = rules if rules is not None else RULES
    last = RuleResult("(empty)", "pass")
    for rule in chain:
        last = rule(session, company, ctx)
        if last.verdict == "stop":
            state = last.state or "parked"
            company.state = state
            company.state_reason = last.reason
            session.flush()
            return last
    company.state = "active"
    company.state_reason = last.reason or "all_rules_passed"
    session.flush()
    return last
