"""Persona classification tests (config/personas.yaml v2, 15 personas, M5)."""
from __future__ import annotations

import pytest

from buyerintel.db.models import Company, CompanyPersona, Persona
from buyerintel.score.personas import (
    classify,
    cosine,
    keyword_hits,
    store_company_personas,
    sync_personas,
    term_vector,
)


@pytest.fixture(autouse=True)
def personas(session):
    assert sync_personas(session) == 15
    session.commit()


def test_sync_is_idempotent(session):
    assert sync_personas(session) == 15
    assert session.query(Persona).count() == 15
    assert sync_personas(session) == 15
    assert session.query(Persona).count() == 15


def test_keyword_hits_match_normalized_text(session):
    p1 = session.query(Persona).filter_by(persona_id="P1").one()
    hits = keyword_hits(p1, "ABC GmbH - spice importer and packer of bulk herbs")
    assert "spice importer" in hits
    assert "bulk" in hits


def test_cosine_identical_and_orthogonal():
    v1 = term_vector("chamomile tea")
    assert cosine(v1, v1) == 1.0
    assert cosine(v1, term_vector("steel factory")) < 0.2


def test_spice_importer_classifies_p1(session):
    company = Company(canonical_name="Mediterranean Spice Importers Ltd",
                      domain="medspice.example",
                      tags=["spice importer", "bulk", "packer"])
    session.add(company)
    session.commit()
    persona, sim, hits = classify(session, company)
    assert persona is not None and persona.persona_id == "P1"
    assert sim >= 0.25
    assert "spice importer" in hits


def test_unrelated_company_classifies_nothing(session):
    company = Company(canonical_name="Joe's Random Stuff")
    session.add(company)
    session.commit()
    persona, sim, hits = classify(session, company)
    assert persona is None or sim < 0.25


def test_store_company_personas_persists_rows(session):
    company = Company(canonical_name="Herbal Tea Blenders Inc",
                      tags=["herbal tea", "tea bag"])
    session.add(company)
    session.commit()
    best, sim = store_company_personas(session, company)
    assert best is not None
    assert best.persona_id == "P5"  # herbal_tea_blender
    rows = session.query(CompanyPersona).filter_by(company_id=company.id).all()
    assert len(rows) >= 1
    assert all(0.0 <= r.similarity <= 1.0 for r in rows)
    # idempotent re-run
    store_company_personas(session, company)
    assert (session.query(CompanyPersona)
            .filter_by(company_id=company.id).count()) == len(rows)
