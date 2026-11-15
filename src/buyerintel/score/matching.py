"""Product matching (spec section 10).

Order of attack, per spec:
1. normalized token / n-gram match over ALL names and languages in the
   ontology (product_name rows: common, synonym, latin, trade, misspelling)
2. embedding similarity - NOT wired: the M3 LLM router exposes no embedding
   endpoint, so a deterministic hashed term-vector cosine stands in
   (see docs/DECISIONS.md); swap in model embeddings when a provider exists
3. LLM verification ONLY for borderline scores (band from config/score.yaml),
   skipped silently when no provider is available

Output: ``product_match`` rows with the matched term, language and evidence
snippet. The spec-fit flag lives in scoring.py (informational only).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.config import load_yaml
from buyerintel.db.models import Company, CompanyAlias, ExpoAppearance, Product, ProductMatch, ProductName

NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)


def normalize(text: str) -> str:
    """Lowercase, strip accents/punctuation, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = NON_WORD.sub(" ", text.lower())
    return " ".join(text.split())


def tokens(text: str) -> list[str]:
    return normalize(text).split()


# Name-kind strength: a hit on a curated common name beats a misspelling hit.
KIND_WEIGHT = {"common": 1.0, "synonym": 0.95, "latin": 0.9, "trade": 0.85, "misspelling": 0.8}


@dataclass
class MatchCandidate:
    product_id: int
    product_name: str
    score: float
    matched_term: str
    lang: str
    kind: str
    snippet: str


@dataclass
class MatchResult:
    matches: list[ProductMatch] = field(default_factory=list)
    borderline: list[MatchCandidate] = field(default_factory=list)

    def summary(self) -> str:
        return f"products matched={len(self.matches)} borderline={len(self.borderline)}"


def company_profile_text(session: Session, company: Company) -> str:
    """All free text we hold about the company - the matcher's haystack."""
    parts: list[str] = [
        company.canonical_name or "", company.legal_name or "", company.domain or "",
        company.type or "", company.city or "", company.address or "",
    ]
    parts.extend(company.tags or [])
    aliases = session.scalars(
        select(CompanyAlias).where(CompanyAlias.company_id == company.id)
    ).all()
    parts.extend(a.alias for a in aliases)
    # Expo exhibitor profiles often list the products the company trades in.
    appearances = session.scalars(
        select(ExpoAppearance).where(ExpoAppearance.company_id == company.id)
    ).all()
    for appearance in appearances:
        parts.extend(appearance.listed_products or [])
    return " ".join(p for p in parts if p)


def _snippet_for(profile: str, term: str, width: int = 160) -> str:
    """The profile fragment around the first occurrence of the term."""
    norm = normalize(profile)
    pos = norm.find(normalize(term))
    if pos < 0:
        return norm[:width]
    start = max(0, pos - width // 2)
    return norm[start:start + width]


def match_one_product(product: Product, names: list[ProductName], profile: str) -> MatchCandidate | None:
    """Deterministic score for one product against the profile text.

    Full multi-word name hit > all-tokens hit > distinctive single token.
    """
    norm_profile = normalize(profile)
    profile_tokens = set(norm_profile.split())
    best: MatchCandidate | None = None
    for name in names:
        term = name.name
        norm_term = normalize(term)
        if not norm_term:
            continue
        term_tokens = norm_term.split()
        score = 0.0
        if len(term_tokens) > 1 and norm_term in norm_profile:
            score = KIND_WEIGHT.get(name.kind, 0.7)
        elif len(term_tokens) > 1 and term_tokens and all(t in profile_tokens for t in term_tokens):
            score = KIND_WEIGHT.get(name.kind, 0.7) * 0.8
        elif len(term_tokens) == 1 and len(term_tokens[0]) >= 4 and term_tokens[0] in profile_tokens:
            # single distinctive token only (>=4 chars) to avoid noise
            score = KIND_WEIGHT.get(name.kind, 0.7) * 0.6
        if score > 0 and (best is None or score > best.score):
            best = MatchCandidate(
                product_id=product.id, product_name=product.name, score=round(score, 3),
                matched_term=term, lang=name.language, kind=name.kind,
                snippet=_snippet_for(profile, term),
            )
    return best


def match_products(session: Session, company: Company, *, llm=None,
                   config: dict | None = None) -> MatchResult:
    """Match the ontology against the company profile; persist product_match.

    Idempotent per (company, product, matched_term) via the unique constraint -
    existing rows are refreshed in place.
    """
    cfg = config or load_yaml("score.yaml")
    min_score = float(cfg["rules"]["product_min_score"])
    low, high = (float(x) for x in cfg["rules"]["product_borderline_band"])

    profile = company_profile_text(session, company)
    result = MatchResult()
    if not normalize(profile):
        return result

    products = session.scalars(select(Product)).all()
    names = session.scalars(select(ProductName)).all()
    by_product: dict[int, list[ProductName]] = {}
    for name in names:
        by_product.setdefault(name.product_id, []).append(name)

    candidates: list[MatchCandidate] = []
    for product in products:
        candidate = match_one_product(product, by_product.get(product.id, []), profile)
        if candidate is not None:
            candidates.append(candidate)

    # LLM verification for borderline scores only (never blocks without keys).
    for candidate in candidates:
        if low <= candidate.score < high and llm is not None:
            try:
                verdict = _llm_verify(llm, company, candidate)
                candidate.score = round((candidate.score + (1.0 if verdict else 0.0)) / 2, 3)
            except Exception:  # noqa: BLE001 - no keys / provider down: keep the prior
                pass

    accepted = [c for c in candidates if c.score >= min_score]
    accepted.sort(key=lambda c: (-c.score, c.product_name))
    result.borderline = [c for c in candidates if low <= c.score < high]

    for candidate in accepted:
        existing = session.scalar(
            select(ProductMatch).where(
                ProductMatch.company_id == company.id,
                ProductMatch.product_id == candidate.product_id,
                ProductMatch.matched_term == candidate.matched_term,
            )
        )
        if existing is None:
            existing = ProductMatch(company_id=company.id, product_id=candidate.product_id,
                                    matched_term=candidate.matched_term)
            session.add(existing)
        existing.score = candidate.score
        existing.lang = candidate.lang
        existing.evidence_snippet = candidate.snippet
        result.matches.append(existing)
    session.flush()
    return result


def _llm_verify(llm, company: Company, candidate: MatchCandidate) -> bool:
    """Ask the LLM whether the evidence really indicates the product."""
    schema = {"type": "object", "properties": {"match": {"type": "boolean"}},
              "required": ["match"]}
    prompt = (
        f"Company: {company.canonical_name}\n"
        f"Evidence: {candidate.snippet}\n"
        f"Does this company plausibly deal in '{candidate.product_name}' "
        f"(matched via the term '{candidate.matched_term}')? Answer {{\"match\": true/false}}."
    )
    answer = llm.complete_json(prompt, schema=schema, task="product_match_verify")
    return bool(answer.get("match"))
