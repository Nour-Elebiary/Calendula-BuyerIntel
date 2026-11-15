"""Social discovery (spec 12.5).

Brand handles on LinkedIn, Facebook, Instagram, TikTok, YouTube, X via the
search router (per-platform `site:` queries from config). Stores results as
contact_point rows of the matching kind. Sherlock/Maigret adapters for
company handles land as optional extras later; this module never logs in.
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.config import load_yaml
from buyerintel.db.models import ContactPoint
from buyerintel.search.router import SearchRouter

HANDLE_RES = {
    "linkedin": re.compile(r"linkedin\.com/company/([A-Za-z0-9_-]{2,})"),
    "facebook": re.compile(r"facebook\.com/([A-Za-z0-9_.-]{2,})"),
    "instagram": re.compile(r"instagram\.com/([A-Za-z0-9_.]{2,})"),
    "tiktok": re.compile(r"tiktok\.com/@([A-Za-z0-9_.]{2,})"),
    "youtube": re.compile(r"youtube\.com/@([A-Za-z0-9_.-]{2,})"),
    "x": re.compile(r"(?:twitter|x)\.com/([A-Za-z0-9_]{2,})"),
}


def discover_socials(session: Session, router: SearchRouter, company_name: str,
                     *, config: dict | None = None) -> list[dict]:
    """Search each platform for the company's brand handle.

    Returns [{platform, handle, url, title, snippet}] - deduplicated.
    """
    cfg = config or load_yaml("enrich.yaml")
    platforms = (cfg.get("social") or {}).get("platforms") or {}
    max_results = int((cfg.get("social") or {}).get("max_results_per_platform", 3))
    found: list[dict] = []
    seen: set[str] = set()
    for platform, template in platforms.items():
        query = template.format(name=company_name)
        try:
            results, _stats = router.search(query, query_type="company", max_results=max_results)
        except Exception:  # noqa: BLE001 - a failed platform never stops discovery
            continue
        pattern = HANDLE_RES.get(platform)
        for result in results:
            match = pattern.search(result.url) if pattern else None
            if not match:
                continue
            handle = match.group(1).lower()
            if handle in seen:
                continue
            seen.add(handle)
            found.append({
                "platform": platform,
                "handle": handle,
                "url": result.url,
                "title": result.title,
                "snippet": result.snippet,
            })
    return found


def store_socials(session: Session, company_id: int, socials: list[dict]) -> int:
    """Upsert discovered socials as contact_point rows (kind = platform)."""
    added = 0
    for social in socials:
        existing = session.scalar(
            select(ContactPoint).where(
                ContactPoint.owner_type == "company",
                ContactPoint.owner_id == company_id,
                ContactPoint.kind == social["platform"],
                ContactPoint.value == social["url"],
            )
        )
        if existing:
            continue
        session.add(
            ContactPoint(
                owner_type="company",
                owner_id=company_id,
                kind=social["platform"],
                value=social["url"],
                source_url=social["url"],
                verified_status="published",
                confidence=0.6,
            )
        )
        added += 1
    session.flush()
    return added
