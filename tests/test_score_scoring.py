"""Scoring, tier assignment and ScoreRun tests (spec section 13, M5).

Weights v2 (5 components, sum 100) from config/weights.yaml; the breakdown
stored in score_run.components must be fully reproducible; tier changes are
written through the audit layer.
"""
from __future__ import annotations

import pytest

from buyerintel.catalog import seed_products
from buyerintel.db.models import (
    ArchiveSnapshot,
    AuditLog,
    Company,
    ContactPoint,
    EnrichCheckpoint,
    Expo,
    ExpoAppearance,
    ExpoEdition,
    Person,
    ScoreRun,
)
from buyerintel.score.personas import sync_personas
from buyerintel.score.scoring import (
    assign_tier,
    load_weights,
    score_company,
    spec_fit_flag,
)

THRESHOLDS = {"tier1": {"min_total": 70, "min_product_fit": 14},
              "tier2": {"min_total": 45, "max_total": 69},
              "tier3": {"max_total": 44}}
WEIGHTS = {"persona_fit_product_overlap": 35, "scale_buying_frequency": 25,
           "certification_alignment": 15, "reachability": 15, "timing_signals": 10}


@pytest.fixture(autouse=True)
def ontology_and_personas(session):
    seed_products(session)
    sync_personas(session)
    session.commit()


def test_weights_sum_to_100():
    version, weights, thresholds = load_weights()
    assert sum(weights.values()) == 100
    assert version >= 2


def test_assign_tier_gate():
    # tier 1 needs total >= 70 AND product-fit >= 14: high total alone is not enough
    comps = {"persona_fit_product_overlap": 10}
    assert assign_tier(85.0, comps, THRESHOLDS) == 2
    comps["persona_fit_product_overlap"] = 20
    assert assign_tier(85.0, comps, THRESHOLDS) == 1
    assert assign_tier(60.0, comps, THRESHOLDS) == 2
    assert assign_tier(30.0, comps, THRESHOLDS) == 3


def test_spec_fit_flag_informational():
    company = Company(canonical_name="Pharma Labs", tags=["ph. eur", "cut size"])
    assert set(spec_fit_flag(company)) >= {"ph_eur", "cut_size"}
    assert spec_fit_flag(Company(canonical_name="Random Co")) == []


def test_score_breakdown_reproducible(session):
    """Same inputs -> identical breakdown twice (M5 acceptance)."""
    company = Company(canonical_name="Nile Spice Importers", domain="nile.example",
                      country="DEU", tags=["spice importer", "bulk", "hibiscus", "organic"])
    session.add(company)
    session.commit()
    run1 = score_company(session, company)
    total1 = run1.total
    run2 = score_company(session, company)
    assert run2.total == total1
    assert run2.components["components"] == run1.components["components"]
    # every component present with its detail
    for name in WEIGHTS:
        assert name in run2.components["components"]
        assert name in run2.components["detail"]


def test_score_persists_and_sets_tier(session):
    company = Company(canonical_name="Herb House", domain="herbhouse.example",
                      tags=["herb importer"])
    session.add(company)
    session.commit()
    run = score_company(session, company)
    session.commit()
    assert run.id is not None
    assert company.tier == run.tier
    assert company.score == run.total
    stored = session.query(ScoreRun).filter_by(company_id=company.id).one()
    assert stored.components["components"]


def test_strong_buyer_reaches_high_score(session):
    """A fully-specified buyer (persona keywords, products, certs, contacts,
    expo) scores high; a bare company scores low."""
    strong = Company(canonical_name="Nile Spice Importers", domain="nile.example",
                     size_band="large", registry_id="HRB123",
                     tags=["spice importer", "herb importer", "packer", "bulk",
                           "container", "organic", "halal", "kosher", "iso",
                           "brcgs", "fda", "gmp", "hibiscus", "chamomile", "karkade"])
    session.add(strong)
    session.flush()
    session.add(Person(company_id=strong.id, full_name="Jane Buyer",
                       title="Purchasing Manager", is_decision_maker=True))
    session.add(ContactPoint(owner_type="company", owner_id=strong.id, kind="email",
                             value="info@nile.example", verified_status="verified",
                             confidence=0.95))
    session.add(ContactPoint(owner_type="company", owner_id=strong.id, kind="phone",
                             value="+49123456", verified_status="published",
                             confidence=0.9))
    session.add(ContactPoint(owner_type="company", owner_id=strong.id, kind="linkedin",
                             value="https://linkedin.com/company/nile", confidence=0.8))
    session.add(EnrichCheckpoint(company_id=strong.id, stage="s4", status="done",
                                 detail={"pages": 6}))
    expo = Expo(name="Anuga")
    session.add(expo)
    session.flush()
    edition = ExpoEdition(expo_id=expo.id, year=__import__("datetime").datetime.now().year)
    session.add(edition)
    session.flush()
    session.add(ExpoAppearance(company_id=strong.id, edition_id=edition.id))
    session.commit()

    weak = Company(canonical_name="Bare Traders")
    session.add(weak)
    session.commit()

    strong_run = score_company(session, strong)
    weak_run = score_company(session, weak)
    assert strong_run.total >= 60.0
    assert weak_run.total < strong_run.total
    # reachability must be full for the strong company
    assert strong_run.components["components"]["reachability"] == 15.0
    # spec-fit recorded (informational), in the run detail
    assert "spec_fit" in strong_run.components


def test_tier_change_is_logged(session):
    """Tier changes flow through the audit layer (M5 acceptance)."""
    company = Company(canonical_name="Rising Trader", tags=["herb importer"])
    session.add(company)
    session.commit()
    run1 = score_company(session, company)
    session.commit()
    old_tier = company.tier

    # company improves: verified contacts + decision maker appear
    session.add(Person(company_id=company.id, full_name="Buyer Boss",
                       is_decision_maker=True))
    session.add(ContactPoint(owner_type="company", owner_id=company.id, kind="email",
                             value="boss@rising.example", verified_status="verified",
                             confidence=0.95))
    session.commit()
    run2 = score_company(session, company)
    session.commit()

    changes = session.query(AuditLog).filter_by(table_name="company", row_id=str(company.id),
                                                field="tier").all()
    # at least the first assignment (None -> tier) is logged
    assert changes, "tier changes must be audited"
    assert run2.components["previous_tier"] == old_tier
