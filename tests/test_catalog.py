from __future__ import annotations

from sqlalchemy import func, select

from buyerintel.catalog import seed_products
from buyerintel.db.models import Product, ProductName


def test_seed_products_upserts_catalog(session):
    stats = seed_products(session)
    session.commit()
    count = session.scalar(select(func.count()).select_from(Product))
    assert count == 42, f"catalog must be 42 products, got {count}"
    assert stats.created == 42
    assert session.scalar(select(func.count()).select_from(ProductName)) > 0


def test_seed_products_is_idempotent(session):
    first = seed_products(session)
    session.commit()
    second = seed_products(session)
    session.commit()
    assert first.created == 42
    assert second.created == 0
    assert second.updated == 42
    assert session.scalar(select(func.count()).select_from(Product)) == 42


def test_seeded_products_have_latin_names_and_flags(session):
    seed_products(session)
    session.commit()
    calendula = session.scalar(select(Product).where(Product.name == "calendula"))
    assert calendula.latin_name == "Calendula officinalis"
    assert calendula.organic is True and calendula.conventional is True
    hibiscus = session.scalar(select(Product).where(Product.name == "hibiscus"))
    assert "TBC" in (hibiscus.forms or [])


def test_product_aliases_stored_as_synonyms(session):
    seed_products(session)
    session.commit()
    synonym_names = {
        pn.name for pn in session.scalars(select(ProductName)).all() if pn.kind == "synonym"
    }
    # spot check a few documented aliases
    assert "karkade" in synonym_names  # hibiscus
    assert "molokhia" in synonym_names  # jew's mallow
    assert "black seed" in synonym_names  # nigella


def test_seed_products_stores_latin_and_misspelling_kinds(session):
    from buyerintel.catalog import seed_products

    seed_products(session)
    session.commit()
    by_kind = {}
    for pn in session.scalars(select(ProductName)).all():
        by_kind.setdefault(pn.kind, set()).add(pn.name)
    assert "hibiscus sabdariffa" in by_kind.get("latin", set())
    assert "hibiscus subdariffa" in by_kind.get("misspelling", set())
    assert "melissa officinanalfs" in by_kind.get("misspelling", set())
    assert "sesamurrindicum" in by_kind.get("misspelling", set())


def test_seed_hs_creates_candidates_and_is_idempotent(session):
    from buyerintel.catalog import seed_hs, seed_products
    from buyerintel.db.models import HsCandidate

    seed_products(session)
    session.commit()
    stats = seed_hs(session)
    session.commit()
    assert stats.hs_candidates > 0
    assert stats.skipped == []
    total = session.scalar(select(func.count()).select_from(HsCandidate))
    # rerun must not duplicate
    stats2 = seed_hs(session)
    session.commit()
    assert stats2.hs_candidates == 0
    assert session.scalar(select(func.count()).select_from(HsCandidate)) == total

    # spot check: sesame primary code
    sesame = session.scalar(select(Product).where(Product.name == "sesame"))
    sesame_codes = {
        c.hs_code: c for c in session.scalars(
            select(HsCandidate).where(HsCandidate.product_id == sesame.id)
        ).all()
    }
    assert "120740" in sesame_codes
    assert sesame_codes["120740"].is_primary is True
    assert sesame_codes["120740"].confidence >= 0.9


def test_seed_hs_reports_unknown_products(session):
    from buyerintel.catalog import seed_hs

    stats = seed_hs(session)  # no products seeded yet
    assert stats.hs_candidates == 0
    assert len(stats.skipped) >= 40  # every product name missing
