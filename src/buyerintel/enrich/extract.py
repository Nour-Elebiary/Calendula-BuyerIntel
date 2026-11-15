"""Extraction (spec 12.2, 12.3).

- Tags: organic, conventional, fair trade, halal, kosher, private label and
  similar, parsed from site text (informational only).
- LLM extraction: structured JSON (sells/buys, segments, certifications,
  locations, people with titles, contacts) validated against a schema via
  the LLM router (corrective retries and fallback chain built in).
"""
from __future__ import annotations

import re

from buyerintel.config import load_yaml
from buyerintel.llm.router import LLMRouter

EXTRACTION_SCHEMA = {
    "type": "object",
    "required": ["sells", "buys"],
    "properties": {
        "sells": {"type": "array", "items": {"type": "string"}},
        "buys": {"type": "array", "items": {"type": "string"}},
        "segments": {"type": "array", "items": {"type": "string"}},
        "certifications": {"type": "array", "items": {"type": "string"}},
        "locations": {"type": "array", "items": {"type": "string"}},
        "people": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["full_name"],
                "properties": {
                    "full_name": {"type": "string"},
                    "title": {"type": "string"},
                    "email": {"type": "string"},
                },
            },
        },
        "contacts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["kind", "value"],
                "properties": {
                    "kind": {"type": "string"},
                    "value": {"type": "string"},
                },
            },
        },
    },
}

EXTRACTION_PROMPT = """You are analyzing the crawled text of a company website.
Extract structured information about what this company SELLS and BUYS.

Text (may be partial, in any language):
---
{text}
---

Rules:
- sells: products/categories they sell or manufacture
- buys: products/categories they purchase (raw materials, ingredients)
- segments: industries they serve
- certifications: only certifications actually mentioned (organic, halal, ...)
- locations: offices/factories/countries mentioned
- people: names with titles found on the site
- contacts: emails, phones, social links found on the site
- Empty arrays when nothing found. Never invent anything."""


def extract_tags(text: str, config: dict | None = None) -> list[str]:
    """Informational tags from site text (spec 12.3). Keyword match, lowercased."""
    cfg = config or load_yaml("enrich.yaml")
    tags_cfg = cfg.get("tags") or {}
    lowered = (text or "").lower()
    tags: list[str] = []
    for tag, keywords in tags_cfg.items():
        if any(str(k).lower() in lowered for k in keywords or []):
            tags.append(tag)
    return sorted(tags)


EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
SOCIAL_RES = {
    "linkedin": re.compile(r"linkedin\.com/(?:company|in)/[A-Za-z0-9_-]+"),
    "facebook": re.compile(r"facebook\.com/[A-Za-z0-9_.-]+"),
    "instagram": re.compile(r"instagram\.com/[A-Za-z0-9_.]+"),
    "tiktok": re.compile(r"tiktok\.com/@[A-Za-z0-9_.]+"),
    "youtube": re.compile(r"youtube\.com/@[A-Za-z0-9_.-]+"),
    "x": re.compile(r"(?:twitter|x)\.com/[A-Za-z0-9_]+"),
}


def regex_contacts(html: str) -> list[dict]:
    """Cheap regex contacts off the raw HTML (emails, phones, social links).

    These are published contacts (verified_status 'published'); the LLM
    extraction adds structured fields on top.
    """
    found: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str) -> None:
        key = (kind, value)
        if key not in seen:
            seen.add(key)
            found.append({"kind": kind, "value": value})

    for email in EMAIL_RE.findall(html or ""):
        add("email", email.lower())
    for kind, pattern in SOCIAL_RES.items():
        for match in pattern.findall(html or ""):
            add(kind, "https://" + match if not match.startswith("http") else match)
    return found


def extract_site(session, llm: LLMRouter, text: str, *, data_class: str = "public") -> dict:
    """Full LLM extraction validated against EXTRACTION_SCHEMA (spec 12.2)."""
    return llm.complete_json(
        EXTRACTION_PROMPT.format(text=text[:12000]),
        schema=EXTRACTION_SCHEMA,
        task="bulk_extraction",
        data_class=data_class,
    )
