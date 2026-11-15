"""Product ontology matcher (spec section 10).

Resolves company text and raw terms to catalog products over all stored name
kinds and languages: exact normalized match first, then token/n-gram scanning,
then fuzzy verification. Borderline fuzzy hits are returned with method
"fuzzy_pending" so an LLM verifier can be plugged in without redesign; nothing
below the threshold is guessed.

Outputs ``product_match`` rows with the matched term, language and evidence.
"""
from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.db.models import Product, ProductMatch, ProductName
from buyerintel.resolve.normalize import normalize_name

FUZZY_THRESHOLD = 0.90
# Inside free text only exact/normalized matches are accepted - fuzzy matching
# there would invent products out of unrelated words.
MAX_NGRAM = 4


@dataclass
class OntologyHit:
    product: Product
    matched_term: str
    kind: str          # common | synonym | latin | misspelling | trade | other-language
    language: str
    score: float       # 0..1
    method: str        # exact | ngram | fuzzy_pending


def load_index(session: Session) -> dict[str, tuple[int, str, str, str]]:
    """normalized name -> (product_id, raw name, kind, language)."""
    index: dict[str, tuple[int, str, str, str]] = {}
    names = session.scalars(select(ProductName)).all()
    for row in names:
        key = normalize_name(row.name)
        if key and key not in index:
            index[key] = (row.product_id, row.name, row.kind, row.language)
    for product in session.scalars(select(Product)).all():
        for kind, value in (("common", product.name), ("latin", product.latin_name)):
            if not value:
                continue
            key = normalize_name(value)
            if key and key not in index:
                index[key] = (product.id, value, kind, "en" if kind == "common" else "la")
    return index


def resolve_term(
    session: Session, term: str, *, fuzzy_threshold: float = FUZZY_THRESHOLD
) -> OntologyHit | None:
    """Resolve a single term (name, Latin name, alias) to a catalog product."""
    key = normalize_name(term)
    if not key:
        return None
    index = load_index(session)
    hit = index.get(key)
    if hit:
        product_id, raw_name, kind, language = hit
        return OntologyHit(
            product=session.get(Product, product_id),
            matched_term=raw_name,
            kind=kind,
            language=language,
            score=1.0,
            method="exact",
        )
    # Fuzzy verification over every stored name; best hit wins if above threshold.
    best: tuple[float, tuple[int, str, str, str]] | None = None
    for candidate_key, entry in index.items():
        score = fuzz.token_set_ratio(key, candidate_key) / 100.0
        if best is None or score > best[0]:
            best = (score, entry)
    if best and best[0] >= fuzzy_threshold:
        product_id, raw_name, kind, language = best[1]
        return OntologyHit(
            product=session.get(Product, product_id),
            matched_term=raw_name,
            kind=kind,
            language=language,
            score=best[0],
            method="fuzzy_pending",  # LLM verification hook (spec section 10)
        )
    return None


def scan_text(
    session: Session, text: str, *, max_ngram: int = MAX_NGRAM
) -> list[OntologyHit]:
    """Find product mentions in free text using greedy longest n-gram matching."""
    tokens = normalize_name(text).split()
    if not tokens:
        return []
    index = load_index(session)
    hits: list[OntologyHit] = []
    i = 0
    while i < len(tokens):
        matched = None
        for n in range(min(max_ngram, len(tokens) - i), 0, -1):
            ngram = " ".join(tokens[i : i + n])
            entry = index.get(ngram)
            if entry:
                matched = (n, entry)
                break
        if matched:
            n, (product_id, raw_name, kind, language) = matched
            product = session.get(Product, product_id)
            if product is not None:
                hits.append(
                    OntologyHit(
                        product=product,
                        matched_term=raw_name,
                        kind=kind,
                        language=language,
                        score=1.0,
                        method="ngram",
                    )
                )
            i += n
        else:
            i += 1
    return hits


def record_matches(
    session: Session,
    company_id: int,
    text: str,
    *,
    evidence_url: str | None = None,
    source: str = "ontology",
) -> list[ProductMatch]:
    """Scan text and persist product_match rows (idempotent by unique key)."""
    session.info["audit_source"] = source
    stored: list[ProductMatch] = []
    for hit in scan_text(session, text):
        snippet_start = text.lower().find(hit.matched_term.split()[0].lower())
        snippet = text[max(0, snippet_start - 40) : snippet_start + 80].strip()
        existing = session.scalar(
            select(ProductMatch).where(
                ProductMatch.company_id == company_id,
                ProductMatch.product_id == hit.product.id,
                ProductMatch.matched_term == hit.matched_term,
            )
        )
        if existing:
            continue
        row = ProductMatch(
            company_id=company_id,
            product_id=hit.product.id,
            score=hit.score,
            matched_term=hit.matched_term,
            lang=hit.language,
            evidence_url=evidence_url,
            evidence_snippet=snippet,
        )
        session.add(row)
        stored.append(row)
    session.flush()
    return stored
