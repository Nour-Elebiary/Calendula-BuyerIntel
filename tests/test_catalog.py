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
