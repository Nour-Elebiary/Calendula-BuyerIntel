"""Rule-chain tests (spec section 14, M5): every rule individually + the chain."""
from __future__ import annotations

import pytest

from buyerintel.catalog import seed_products
from buyerintel.db.models import (
    Company,
    ContactPoint,
    EnrichCheckpoint,
    Person,
)
from buyerintel.score import rules as score_rules
from buyerintel.score.personas import sync_personas
from buyerintel.score.rules import (
    RULES,
    competitor_check,
    contact_confidence,
    dedupe_check,
    enrichment_depth,
    final_state,
    liveness,
    load_context,
    persona_match,
    product_fit,
    run_rule_chain,
    scoring_and_tier,
)


@pytest.fixture(autouse=True)
def ontology_and_personas(session):
    seed_products(session)
    sync_personas(session)
    session.commit()


@pytest.fixture()
def ctx():
    return load_context()


def _checkpoint(session, company, stage="s4", detail=None):
    session.add(EnrichCheckpoint(company_id=company.id, stage=stage, status="done",
                                 detail=detail or {"pages": 5}))
    session.commit()


def test_rules_are_ordered_per_spec():
    names = [r.__name__ for r in RULES]
    assert names == ["dedupe_check", "liveness", "competitor_check", "persona_match",
                     "product_fit", "scoring_and_tier", "enrichment_depth",
                     "contact_confidence", "final_state"]


def test_rule1_dedupe_parks_duplicates(session, ctx):
    a = Company(canonical_name="Same Co", domain="same.example")
    b = Company(canonical_name="Other Co", domain="same.example")
    session.add_all([a, b])
    session.commit()
    result = dedupe_check(session, b, ctx)
    assert result.verdict == "stop" and result.state == "parked"
    assert result.detail["duplicate_of"] == a.id
    # a itself passes (checked first, no earlier duplicate)
    assert dedupe_check(session, a, ctx).verdict == "pass"


def test_rule2_liveness_defunct_vs_no_website(session, ctx):
    dead = Company(canonical_name="Dead Co", domain="dead.example")
    session.add(dead)
    session.commit()
    _checkpoint(session, dead, detail={"pages": 0, "note": "no page fetched"})
    result = liveness(session, dead, ctx)
    assert result.state == "excluded" and result.reason == "defunct"

    offweb = Company(canonical_name="Offweb Traders")
    session.add(offweb)
    session.commit()
    result2 = liveness(session, offweb, ctx)
    assert result2.state == "parked" and result2.reason == "no_website"

    alive = Company(canonical_name="Alive Co", domain="alive.example", registry_id="HRB1")
    session.add(alive)
    session.commit()
    assert liveness(session, alive, ctx).verdict == "pass"


def test_rule3_competitor_excluded_and_tagged(session, ctx):
    rival = Company(canonical_name="Egypt Herbs Export Co", type="exporter")
    session.add(rival)
    session.commit()
    result = competitor_check(session, rival, ctx)
    assert result.state == "excluded" and result.reason == "competitor"
    assert "competitor-intel" in (rival.tags or [])

    buyer = Company(canonical_name="German Herb Import GmbH", type="exporter",
                    tags=["importer", "purchasing office"])
    session.add(buyer)
    session.commit()
    assert competitor_check(session, buyer, ctx).verdict == "pass"

    neutral = Company(canonical_name="Corner Bakery Shop")
    session.add(neutral)
    session.commit()
    assert competitor_check(session, neutral, ctx).verdict == "pass"


def test_rule4_persona_parks_unclear(session, ctx):
    unclear = Company(canonical_name="Joe's Random Stuff")
    session.add(unclear)
    session.commit()
    result = persona_match(session, unclear, ctx)
    assert result.state == "parked" and result.reason == "persona_unclear"

    clear = Company(canonical_name="Spice Import Trading GmbH",
                    tags=["spice importer", "bulk"])
    session.add(clear)
    session.commit()
    ok = persona_match(session, clear, ctx)
    assert ok.verdict == "pass" and ok.detail["persona"] == "P1"


def test_rule5_product_fit_parks_no_fit(session, ctx):
    no_fit = Company(canonical_name="Unclear Traders",
                     tags=["spice importer", "bulk", "trading"])
    session.add(no_fit)
    session.commit()
    result = product_fit(session, no_fit, ctx)
    assert result.state == "parked" and result.reason == "no_product_fit"

    fit = Company(canonical_name="Hibiscus Traders",
                  tags=["spice importer", "bulk", "hibiscus"])
    session.add(fit)
    session.commit()
    assert product_fit(session, fit, ctx).verdict == "pass"


def test_rule6_scoring_assigns_tier_and_persists(session, ctx):
    company = Company(canonical_name="Scoring Test Co",
                      tags=["spice importer", "bulk", "hibiscus"])
    session.add(company)
    session.commit()
    result = scoring_and_tier(session, company, ctx)
    assert result.verdict == "pass"
    assert company.tier == result.detail["tier"]
    assert result.detail["components"]["persona_fit_product_overlap"] > 0


def test_rule7_enrichment_depth_paid_gate_tier1_only(session, ctx):
    t1 = Company(canonical_name="Tier One Co", tier=1)
    t3 = Company(canonical_name="Tier Three Co", tier=3)
    session.add_all([t1, t3])
    session.commit()
    gate1 = enrichment_depth(session, t1, ctx)
    gate3 = enrichment_depth(session, t3, ctx)
    assert gate1.detail["paid_tool_gate"]["allowed"] is True
    assert gate3.detail["paid_tool_gate"]["allowed"] is False
    assert gate3.detail["paid_tool_gate"]["status"] == "never_fires"


def test_rule8_low_contact_confidence_needs_enrichment(session, ctx):
    weak = Company(canonical_name="Weak Contacts Co")
    session.add(weak)
    session.flush()
    session.add(ContactPoint(owner_type="company", owner_id=weak.id, kind="email",
                             value="guess@weak.example", verified_status="pattern-guessed",
                             confidence=0.3))
    session.commit()
    result = contact_confidence(session, weak, ctx)
    assert result.state == "needs_enrichment" and result.reason == "low_contact_confidence"

    strong = Company(canonical_name="Strong Contacts Co")
    session.add(strong)
    session.flush()
    session.add(ContactPoint(owner_type="company", owner_id=strong.id, kind="email",
                             value="info@strong.example", verified_status="published",
                             confidence=0.9))
    session.commit()
    assert contact_confidence(session, strong, ctx).verdict == "pass"


def test_rule9_final_state_active(session, ctx):
    company = Company(canonical_name="Final Co")
    session.add(company)
    session.commit()
    result = final_state(session, company, ctx)
    assert result.state == "active"


def test_full_chain_strong_buyer_ends_active(session, ctx):
    company = Company(canonical_name="Nile Spice Importers", domain="nile.example",
                      registry_id="HRB123",
                      tags=["spice importer", "bulk", "hibiscus", "organic", "halal"])
    session.add(company)
    session.flush()
    session.add(Person(company_id=company.id, full_name="Jane Buyer",
                       is_decision_maker=True))
    session.add(ContactPoint(owner_type="company", owner_id=company.id, kind="email",
                             value="info@nile.example", verified_status="published",
                             confidence=0.9))
    session.commit()
    _checkpoint(session, company)

    result = run_rule_chain(session, company, ctx)
    session.commit()
    assert result.state == "active"
    assert company.state == "active"
    assert company.state_reason == "all_rules_passed"
    assert company.tier is not None and company.score is not None


def test_full_chain_stops_at_defunct(session, ctx):
    dead = Company(canonical_name="Dead Importer", domain="dead.example",
                   tags=["spice importer", "bulk", "hibiscus"])
    session.add(dead)
    session.commit()
    _checkpoint(session, dead, detail={"pages": 0, "note": "no page fetched"})

    result = run_rule_chain(session, dead, ctx)
    assert result.state == "excluded" and result.reason == "defunct"
    assert dead.state == "excluded(defunct)" or dead.state == "excluded"
    assert dead.state_reason == "defunct"
    # later rules never ran: no score run exists
    assert dead.score is None


def test_full_chain_low_contacts_ends_needs_enrichment(session, ctx):
    company = Company(canonical_name="Thin Contacts Importers", domain="thin.example",
                      tags=["spice importer", "bulk", "hibiscus"])
    session.add(company)
    session.flush()
    session.add(EnrichCheckpoint(company_id=company.id, stage="s4", status="done",
                                 detail={"pages": 3}))
    session.add(ContactPoint(owner_type="company", owner_id=company.id, kind="email",
                             value="guess@thin.example", verified_status="pattern-guessed",
                             confidence=0.3))
    session.commit()
    result = run_rule_chain(session, company, ctx)
    assert result.state == "needs_enrichment"
    assert company.state_reason == "low_contact_confidence"
