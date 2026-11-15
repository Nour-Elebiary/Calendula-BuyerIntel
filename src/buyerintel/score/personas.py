"""Persona classification and similarity (config/personas.yaml v2, 15 personas).

The persona doc (section 9) defines keyword + HS-code classification per
persona. Embeddings would be the ideal similarity, but no embedding provider
is wired (M3 LLM router has none), so similarity is a deterministic blend:

  similarity = 0.5 * keyword_hit_ratio + 0.5 * hashed-term-vector cosine

Same blend a real embedding model approximates; swap in model embeddings when
a provider exists (the ``persona.embedding`` column is ready for it).
"""
from __future__ import annotations

import hashlib
import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.config import load_yaml
from buyerintel.db.models import Company, CompanyPersona, Persona
from buyerintel.score.matching import company_profile_text, normalize, tokens


def sync_personas(session: Session, *, source: str = "personas.yaml") -> int:
    """Upsert personas.yaml -> persona table. Idempotent by persona_id."""
    session.info["audit_source"] = f"sync:{source}"
    data = load_yaml(source)
    count = 0
    for entry in data.get("personas", []):
        pid = str(entry["id"])
        persona = session.scalar(select(Persona).where(Persona.persona_id == pid))
        if persona is None:
            persona = Persona(persona_id=pid)
            session.add(persona)
        persona.name = str(entry.get("name", pid))
        persona.definition = entry
        count += 1
    session.flush()
    return count


def persona_text(persona: Persona) -> str:
    """All textual signal of a persona, for vector comparison."""
    d = persona.definition or {}
    parts = [d.get("name", ""), persona.name, d.get("group", ""), d.get("product_mix", "")]
    parts.extend(d.get("keywords", []))
    parts.extend(d.get("target_titles", []))
    parts.extend(d.get("cares_about", []))
    return " ".join(str(p) for p in parts if p)


def term_vector(text: str, dims: int = 512) -> dict[int, float]:
    """Hashed bag-of-words vector, L2-normalized. Deterministic, no deps."""
    vec: dict[int, float] = {}
    for tok in tokens(text):
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16) % dims
        vec[h] = vec.get(h, 0.0) + 1.0
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {k: v / norm for k, v in vec.items()}


def cosine(a: dict[int, float], b: dict[int, float]) -> float:
    return round(sum(v * b.get(k, 0.0) for k, v in a.items()), 4)


def keyword_hits(persona: Persona, company_text: str) -> list[str]:
    """Persona keywords appearing in the company text (normalized substring)."""
    norm = f" {normalize(company_text)} "
    hits = []
    for keyword in (persona.definition or {}).get("keywords", []):
        if f" {normalize(str(keyword))} " in norm or normalize(str(keyword)) in norm:
            hits.append(str(keyword))
    return hits


def classify(session: Session, company: Company, *, config: dict | None = None) -> tuple[Persona | None, float, list[str]]:
    """Best persona for the company: (persona, similarity, keyword_hits).

    similarity = 0.5 * hit_ratio + 0.5 * cosine(persona vector, company vector),
    boosted 0.1 per extra keyword hit, capped at 1.0.
    """
    cfg = config or load_yaml("score.yaml")
    min_sim = float(cfg["rules"]["persona_min_similarity"])

    profile = company_profile_text(session, company)
    company_vec = term_vector(profile)

    best: tuple[Persona | None, float, list[str]] = (None, 0.0, [])
    for persona in session.scalars(select(Persona)).all():
        hits = keyword_hits(persona, profile)
        hit_ratio = len(hits) / max(1, len((persona.definition or {}).get("keywords", [])))
        sim = 0.5 * hit_ratio + 0.5 * cosine(term_vector(persona_text(persona)), company_vec)
        sim = min(1.0, sim + 0.1 * max(0, len(hits) - 1))
        if sim > best[1]:
            best = (persona, round(sim, 4), hits)

    persona, sim, hits = best
    if persona is None or sim < min_sim:
        return (None, round(sim, 4), hits) if persona else (None, 0.0, hits)
    return persona, sim, hits


def store_company_personas(session: Session, company: Company) -> tuple[Persona | None, float]:
    """Classify and persist every persona with similarity > 0 (unique per pair)."""
    sync_personas(session)
    best_persona, best_sim, best_hits = classify(session, company)

    # Vector similarities for all personas (persisted; cheap: 15 personas).
    profile = company_profile_text(session, company)
    company_vec = term_vector(profile)
    for persona in session.scalars(select(Persona)).all():
        hits = keyword_hits(persona, profile)
        hit_ratio = len(hits) / max(1, len((persona.definition or {}).get("keywords", [])))
        sim = 0.5 * hit_ratio + 0.5 * cosine(term_vector(persona_text(persona)), company_vec)
        sim = min(1.0, sim + 0.1 * max(0, len(hits) - 1))
        row = session.scalar(
            select(CompanyPersona).where(
                CompanyPersona.company_id == company.id,
                CompanyPersona.persona_id == persona.id,
            )
        )
        if row is None:
            row = CompanyPersona(company_id=company.id, persona_id=persona.id)
            session.add(row)
        row.similarity = round(sim, 4)
    session.flush()
    return best_persona, best_sim
