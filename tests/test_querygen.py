"""Query builder tests (spec section 8, query generation)."""
from __future__ import annotations

import pytest

from buyerintel.catalog import seed_products
from buyerintel.search.querygen import (
    COUNTRY_LANG,
    ROLE_WORDS,
    build_company_plan,
    company_name_variants,
    product_terms,
)


def test_company_name_variants():
    assert company_name_variants("Herbhandel GmbH") == ["Herbhandel GmbH"]
    variants = company_name_variants("Herbhandel GmbH", domain="herbhandel.de", city="Hamburg")
    assert variants == ["Herbhandel GmbH", "Herbhandel GmbH Hamburg", "herbhandel.de"]


def test_plan_has_contact_and_operator_patterns():
    plan = build_company_plan("Herbhandel GmbH", country="DEU")
    texts = [q.text for q in plan.queries]
    assert "Herbhandel GmbH DEU contact" in texts
    assert "Herbhandel GmbH email" in texts
    assert "Herbhandel GmbH filetype:pdf catalog" in texts


def test_plan_role_queries_in_local_language():
    plan = build_company_plan("Herbhandel GmbH", country="DEU")
    de_texts = [q.text for q in plan.queries if q.language == "de"]
    joined = " | ".join(de_texts)
    assert "Herbhandel GmbH Einkauf" in joined  # spec's example role word
    en_texts = [q.text for q in plan.queries if q.language == "en"]
    assert any("purchasing" in t for t in en_texts)


def test_plan_person_query_with_title():
    plan = build_company_plan("Herbhandel GmbH", country="DEU", title="Head of Purchasing")
    texts = [q.text for q in plan.queries]
    assert "site:linkedin.com/in Head of Purchasing Herbhandel GmbH" in texts


def test_plan_country_engine_hints():
    plan = build_company_plan("上海草藥", country="CHN")
    baidu_queries = [q for q in plan.queries if "baidu" in q.engines]
    assert baidu_queries


def test_plan_product_queries_from_ontology(session):
    seed_products(session)
    session.commit()
    plan = build_company_plan("Buyer BV", country="NLD", session=session, products=["hibiscus"])
    product_queries = [q for q in plan.queries if q.query_type == "product"]
    terms = [q.text for q in product_queries]
    assert any("karkade" in t for t in terms)          # synonym
    assert any("hibiscus sabdariffa" in t for t in terms)  # latin


def test_role_words_cover_spec_examples():
    # the exact role words the spec lists must be present
    assert "Einkauf" in ROLE_WORDS["de"]
    assert "achats" in ROLE_WORDS["fr"]
    assert "compras" in ROLE_WORDS["es"]
    assert "acquisti" in ROLE_WORDS["it"]
    assert "inkoop" in ROLE_WORDS["nl"]
    assert "采购" in ROLE_WORDS["zh"]
    assert "調達" in ROLE_WORDS["ja"]
    assert "закупки" in ROLE_WORDS["ru"]
    assert "satın alma" in ROLE_WORDS["tr"]
    assert "مشتريات" in ROLE_WORDS["ar"]


def test_country_language_map():
    assert COUNTRY_LANG["DEU"] == "de"
    assert COUNTRY_LANG["USA"] == "en"
