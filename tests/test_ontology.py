"""M2 acceptance: the ontology resolves common and Latin names on a fixture set."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from buyerintel.catalog import seed_products
from buyerintel.db.models import ProductMatch
from buyerintel.ontology import record_matches, resolve_term, scan_text


@pytest.fixture()
def catalog(session):
    seed_products(session)
    session.commit()
    return session


def _product(hit):
    return hit.product.name if hit else None


def test_resolves_common_names(catalog):
    assert _product(resolve_term(catalog, "hibiscus")) == "hibiscus"
    assert _product(resolve_term(catalog, "chamomile")) == "chamomile"


def test_resolves_latin_names(catalog):
    hit = resolve_term(catalog, "Hibiscus sabdariffa")
    assert _product(hit) == "hibiscus"
    assert hit.kind == "latin"


def test_resolves_synonym_families(catalog):
    assert _product(resolve_term(catalog, "karkade")) == "hibiscus"
    assert _product(resolve_term(catalog, "kalonji")) == "nigella"
    assert _product(resolve_term(catalog, "methi")) == "fenugreek"
    assert _product(resolve_term(catalog, "licorice")) in {"liquorice"}
    assert _product(resolve_term(catalog, "molokhia")) in {"jew's mallow", "jews mallow"}


def test_resolves_documented_misspellings(catalog):
    assert _product(resolve_term(catalog, "hibiscus subdariffa")) == "hibiscus"
    assert _product(resolve_term(catalog, "melissa officinanalfs")) == "lemon balm"


def test_unrelated_term_returns_none(catalog):
    assert resolve_term(catalog, "steel coils") is None


def test_scan_text_finds_multiple_products(catalog):
    hits = scan_text(catalog, "Quarterly offer: dried karkade and molokhia leaves, plus 120740 sesame")
    found = {h.product.name for h in hits}
    assert "hibiscus" in found
    assert any("mallow" in p or p == "molokhia" for p in found)


def test_record_matches_idempotent(catalog):
    from buyerintel.db.models import Company

    company = Company(canonical_name="Test Buyer GmbH", country="DE")
    catalog.add(company)
    catalog.flush()
    text = "We import dried karkade and molokhia regularly."
    first = record_matches(catalog, company.id, text, evidence_url="https://example.com")
    catalog.commit()
    second = record_matches(catalog, company.id, text)
    catalog.commit()
    assert len(first) >= 2
    assert second == []
    rows = catalog.scalars(
        select(ProductMatch).where(ProductMatch.company_id == company.id)
    ).all()
    assert all(r.evidence_url == "https://example.com" for r in rows)
