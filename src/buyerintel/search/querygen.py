"""Multilingual search query builder (spec section 8, query generation).

Builds query plans for a company, person or product: name variants, role
queries in local languages, ontology product queries in every stored language,
and the operator patterns from the spec (`site:`, `filetype:`, email, catalog).
Search the company's own language and script as well as English.

Translation is offline-first (Argos Translate when installed, then
LibreTranslate, then the LLM router) and disabled by default until the user
configures it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.config import load_yaml
from buyerintel.db.models import ProductName

# Role words in local languages (spec section 8): "purchasing/procurement" terms.
ROLE_WORDS = {
    "en": ["purchasing", "procurement", "buyer", "purchase manager"],
    "de": ["Einkauf", "Einkaufsleiter"],
    "fr": ["achats", "responsable achats"],
    "es": ["compras", "jefe de compras"],
    "it": ["acquisti", "responsabile acquisti"],
    "nl": ["inkoop", "inkoper"],
    "zh": ["采购", "采购经理"],
    "ja": ["調達", "購買"],
    "ru": ["закупки", "руководитель закупок"],
    "tr": ["satın alma"],
    "ar": ["مشتريات", "مدير المشتريات"],
    "ko": ["구매", "구매담당"],
    "pl": ["zakupy"],
    "sv": ["inköp"],
    "el": ["αγορές"],
}

# Country-code -> primary query language (drives role-word choice + engine hints).
COUNTRY_LANG = {
    "DEU": "de", "AUT": "de", "CHE": "de",
    "FRA": "fr", "BEL": "fr",
    "ESP": "es", "MEX": "es",
    "ITA": "it",
    "NLD": "nl",
    "BRA": "pt", "PRT": "pt",
    "RUS": "ru",
    "CHN": "zh",
    "JPN": "ja",
    "KOR": "ko",
    "TUR": "tr",
    "SAU": "ar", "ARE": "ar",
    "POL": "pl",
    "SWE": "sv",
    "GRC": "el",
    "GBR": "en", "USA": "en", "CAN": "en", "AUS": "en", "IND": "en",
}

# Country -> engine preference (spec section 8 table).
COUNTRY_ENGINES = {
    "RUS": ["yandex", "duckduckgo"],
    "TUR": ["yandex", "duckduckgo"],
    "CHN": ["baidu", "duckduckgo"],
    "KOR": ["naver", "duckduckgo"],
    "JPN": ["yahoo_japan", "duckduckgo"],
}


@dataclass
class SearchQuery:
    text: str
    language: str = "en"
    query_type: str = "general"   # company | contact | person | product | catalog
    engines: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"text": self.text, "language": self.language,
                "query_type": self.query_type, "engines": self.engines}


@dataclass
class QueryPlan:
    subject: str                 # company name / person title / product name
    country: str | None
    queries: list[SearchQuery] = field(default_factory=list)

    def as_dicts(self) -> list[dict]:
        return [q.as_dict() for q in self.queries]


def company_name_variants(name: str, *, domain: str | None = None, city: str | None = None) -> list[str]:
    """Name + domain + name-city combos (spec: legal, trade, brand, name plus city, domain only)."""
    variants = [name]
    if city:
        variants.append(f"{name} {city}")
    if domain:
        variants.append(domain)
    return variants


def product_terms(session: Session, product_name: str, *, max_languages: int = 16) -> list[tuple[str, str]]:
    """(term, language) pairs for a product from the ontology, across stored languages."""
    product_names = session.scalars(
        select(ProductName).where(ProductName.name == product_name.strip().lower())
    ).all()
    terms: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    if product_names:
        product_id = product_names[0].product_id
        rows = session.scalars(
            select(ProductName).where(ProductName.product_id == product_id)
        ).all()
        for row in rows:
            key = (row.name, row.language)
            if key not in seen:
                seen.add(key)
                terms.append(key)
    return terms[:max_languages]


def build_company_plan(
    company_name: str,
    *,
    country: str | None = None,
    domain: str | None = None,
    city: str | None = None,
    title: str | None = None,
    session: Session | None = None,
    products: list[str] | None = None,
) -> QueryPlan:
    """Full query plan: identity, contact, person, and product queries."""
    lang = COUNTRY_LANG.get(country or "", "en")
    engines = list(COUNTRY_ENGINES.get(country or "", [])) + ["duckduckgo", "google"]
    plan = QueryPlan(subject=company_name, country=country)
    seen: set[str] = set()

    def add(text: str, query_type: str, language: str = "en", engine_list: list[str] | None = None) -> None:
        text = " ".join(text.split())
        if text and text.lower() not in seen:
            seen.add(text.lower())
            plan.queries.append(
                SearchQuery(text=text, language=language, query_type=query_type,
                            engines=engine_list or engines)
            )

    # 1. Identity: name variants in English and the local language/script.
    for variant in company_name_variants(company_name, domain=domain, city=city):
        add(variant, "company", "en")
    if lang != "en":
        add(company_name, "company", lang)

    # 2. Contact patterns (spec: `{name} {country} contact`, `{name} email`).
    if country:
        add(f"{company_name} {country} contact", "contact", "en")
    else:
        add(f"{company_name} contact", "contact", "en")
    add(f"{company_name} email", "contact", "en")
    add(f"{company_name} filetype:pdf catalog", "catalog", "en")

    # 3. Role queries in the local language + English (spec: Einkauf, achats, ...).
    if title:
        add(f"site:linkedin.com/in {title} {company_name}", "person", "en")
    for role in ROLE_WORDS.get("en", []):
        add(f"{company_name} {role}", "person", "en")
    for role in ROLE_WORDS.get(lang, []):
        add(f"{company_name} {role}", "person", lang)

    # 4. Product queries from the ontology in each target language.
    if session is not None and products:
        for product in products:
            for term, term_lang in product_terms(session, product):
                add(f"{company_name} {term}", "product", term_lang)
    return plan
