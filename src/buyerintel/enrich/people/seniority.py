"""Seniority classification and decision-maker logic (spec section 12.6).

Titles classify into exactly the spec's buckets:

  owner_c_level | vp_director | head_manager | buyer_specialist | other

Decision-makers are marked using the persona's target titles (purchasing,
procurement, sourcing, raw-materials buyer, category manager herbs and
spices, QA / R&D for technical buying, owner for small firms) IN LOCAL
LANGUAGES - the role-word lists below cover the languages from the spec's
query-generation section (Einkauf, achats, compras, acquisti, inkoop,
Chinese, Arabic, Russian, Turkish, ...) plus the persona target titles of
the company's best-matching persona.
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.db.models import Company, CompanyPersona, Person, Persona
from buyerintel.score.matching import normalize

# Seniority buckets in priority order: the first bucket whose keywords hit
# the title wins (a "Chief Purchasing Officer" is owner_c_level, not buyer).
SENIORITY_KEYWORDS: dict[str, list[str]] = {
    "owner_c_level": [
        "owner", "founder", "co-founder", "cofounder", "ceo", "chief", "cfo",
        "coo", "cto", "managing director", "geschaeftsfuehrer", "geschäftsführer",
        "presidente", "direktur", "presidente", "president", "md", "general manager",
        "generaldirektor", "principal", "partner", "proprietor",
    ],
    "vp_director": [
        "vp ", "vice president", "director", "direktor", "direktorin",
        "directeur", "directora", "direttore", "head of", "leiter", "leiterin",
    ],
    "head_manager": [
        "manager", "managerin", "management", "mgr", "lead", "teamlead",
        "supervisor", "koordinator", "coordinator",
    ],
    "buyer_specialist": [
        "buyer", "purchasing", "procurement", "sourcing", "category buyer",
        "category manager", "einkauf", "einkaufer", "einkäufer", "achats",
        "achat", "compras", "acquisti", "inkoop", "inkoper", "zairyo",
        "specialist", "specialiste", "specjalista", "qa", "quality", "r&d",
        "rd", "product manager", "merchandiser", "import manager",
        "importmanager", "trader", "broker",
    ],
    "other": [],
}

# Buying/technical role words in the spec's query languages (section 8):
# a hit marks the person buying-relevant regardless of seniority bucket.
BUYING_ROLE_WORDS = [
    "purchasing", "procurement", "sourcing", "buying", "buyer", "einkauf",
    "achats", "compras", "acquisti", "inkoop", "aankoop", "inköp", "inkop",
    "satın alma", "satinalma", "закупки", "zakupki", "采购", "購買", "購買",
    "مشتريات", "شراء", "cấp", "mua sắm", "pembelian", "procuremente",
    "raw material", "raw-material", "category manager", "qa manager",
    "quality assurance", "r&d", "research and development",
]

# Department guesses from title words (informational; Person.department).
DEPARTMENT_WORDS = {
    "purchasing": ["purchasing", "procurement", "einkauf", "achats", "compras",
                   "acquisti", "inkoop", "采购", "مشتريات", "закупки"],
    "quality": ["qa", "quality", "laboratory", "labor", "compliance"],
    "r&d": ["r&d", "research", "development", "npd", "innovation"],
    "sales": ["sales", "vertrieb", "commercial", "account", "business development"],
    "management": ["management", "geschäftsführung", "executive", "office"],
    "logistics": ["logistics", "supply chain", "export", "shipping"],
}


def classify_title(title: str | None) -> tuple[str, str | None]:
    """(seniority bucket, department-guess) from a raw title.

    Never raises; a missing/unrecognized title is ('other', None).
    Short acronyms (ceo, cfo, cto, md, ...) match on word boundaries only -
    'director' must never trip 'cto' (direc-tor).
    """
    if not title:
        return "other", None
    norm = normalize(title)
    for bucket, keywords in SENIORITY_KEYWORDS.items():
        for keyword in keywords:
            if _keyword_hit(norm, keyword):
                return bucket, _department(norm)
    return "other", _department(norm)


_ASCII_SHORT = re.compile(r"^[a-z]{1,3}$")


def _keyword_hit(norm_title: str, keyword: str) -> bool:
    """Substring hit; word-boundary hit for short ASCII acronyms."""
    norm_kw = normalize(keyword)
    if not norm_kw or norm_kw not in norm_title:
        return False
    if _ASCII_SHORT.match(norm_kw):
        return re.search(rf"\b{re.escape(norm_kw)}\b", norm_title) is not None
    return True


def _department(norm_title: str) -> str | None:
    for department, words in DEPARTMENT_WORDS.items():
        if any(normalize(w) in norm_title for w in words):
            return department
    return None


def is_buying_role(title: str | None) -> bool:
    """Buying/technical relevance in any of the spec's languages."""
    if not title:
        return False
    norm = normalize(title)
    return any(normalize(w) and normalize(w) in norm for w in BUYING_ROLE_WORDS)


def is_decision_maker(title: str | None, persona_target_titles: list[str] | None,
                      *, small_firm: bool = False) -> bool:
    """Spec 12.6: decision-makers via the persona's target titles.

    - title matches one of the persona's target titles (substring, normalized)
    - OR a buying/technical role word appears in the title
    - OR (owner and the firm is small: 'owner for small firms')
    """
    if not title:
        return False
    norm = normalize(title)
    for target in persona_target_titles or []:
        t = normalize(str(target))
        if t and (t in norm or norm in t):
            return True
    if is_buying_role(title):
        return True
    if small_firm:
        bucket, _ = classify_title(title)
        if bucket == "owner_c_level":
            return True
    return False


def best_persona_titles(session: Session, company: Company) -> list[str]:
    """Target titles of the company's best-matching persona (highest similarity)."""
    row = session.scalars(
        select(CompanyPersona).where(CompanyPersona.company_id == company.id)
        .order_by(CompanyPersona.similarity.desc())
    ).first()
    if row is None:
        return []
    persona = session.get(Persona, row.persona_id)
    return list((persona.definition or {}).get("target_titles") or []) if persona else []


def apply_seniority(session: Session, company: Company, *, config: dict | None = None) -> dict:
    """Classify every person of the company; mark decision-makers.

    Idempotent: recomputes seniority/title_normalized/is_decision_maker for
    all existing Person rows. Returns a small stats dict.
    """
    targets = best_persona_titles(session, company)
    small = (company.size_band or "").lower() == "small"
    people = session.scalars(select(Person).where(Person.company_id == company.id)).all()
    stats = {"people": len(people), "decision_makers": 0}
    for person in people:
        person.title_normalized = normalize(person.title or "") or None
        bucket, department = classify_title(person.title)
        person.seniority = bucket
        person.department = person.department or department
        person.is_decision_maker = is_decision_maker(person.title, targets, small_firm=small)
        if person.is_decision_maker:
            stats["decision_makers"] += 1
    session.flush()
    return stats
