"""OSINT adapters (spec 12 / M4): archive.org snapshots + job-post signals.

- archive: Wayback Machine availability + snapshot list for a domain,
  stored in `archive_snapshot` (history of what the site offered over time).
- jobs: purchasing-role job posts as an expansion signal, via the search
  router (JobSpy-style site queries; the JobSpy library adapter lands when
  job volume justifies it).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.config import load_yaml
from buyerintel.db.models import ArchiveSnapshot
from buyerintel.search.router import SearchRouter

USER_AGENT = "Calendula-BuyerIntel/0.1 (local personal research)"
WAYBACK_AVAILABILITY = "https://archive.org/wayback/available"
WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"


def fetch_archive_snapshots(domain: str, *, limit: int = 8,
                            timeout: float = 30.0) -> list[dict]:
    """Wayback CDX: the oldest-to-newest homepage snapshots for a domain."""
    response = httpx.get(
        WAYBACK_CDX,
        params={"url": domain, "output": "json", "fl": "timestamp,original,statuscode",
                "collapse": "timestamp:4", "limit": str(limit)},
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
    )
    response.raise_for_status()
    rows = response.json() or []
    out: list[dict] = []
    for row in rows[1:] if rows and rows[0] and rows[0][0] == "timestamp" else rows:
        if not row or len(row) < 2:
            continue
        out.append({"timestamp": row[0], "url": row[1],
                    "statuscode": row[2] if len(row) > 2 else None})
    return out


def store_archive(session: Session, domain: str, snapshots: list[dict]) -> int:
    """Persist snapshots (dedup by timestamp). Returns rows added."""
    from datetime import datetime as _dt

    added = 0
    for snap in snapshots:
        raw_ts = str(snap.get("timestamp") or "")
        try:
            stamp = _dt.strptime(raw_ts, "%Y%m%d%H%M%S") if raw_ts else None
        except ValueError:
            stamp = None
        if stamp is None:
            continue
        existing = session.scalar(
            select(ArchiveSnapshot).where(ArchiveSnapshot.url == snap.get("url"),
                                          ArchiveSnapshot.timestamp == stamp)
        )
        if existing:
            continue
        session.add(
            ArchiveSnapshot(
                url=snap.get("url") or f"https://{domain}",
                timestamp=stamp,
                source="wayback",
                content_hash=hashlib.sha256(raw_ts.encode()).hexdigest(),
                extracted_json=json.dumps(snap),
            )
        )
        added += 1
    session.flush()
    return added


def latest_snapshot_url(domain: str, *, timeout: float = 15.0) -> str | None:
    """The most recent archived homepage URL (Wayback availability API)."""
    try:
        response = httpx.get(WAYBACK_AVAILABILITY, params={"url": domain},
                             headers={"User-Agent": USER_AGENT}, timeout=timeout)
        response.raise_for_status()
        snapshot = ((response.json() or {}).get("archived_snapshots") or {}).get("closest")
        return snapshot.get("url") if snapshot else None
    except (httpx.HTTPError, ValueError):
        return None


def job_post_signals(session: Session, router: SearchRouter, company_name: str,
                     *, config: dict | None = None) -> list[dict]:
    """Search for purchasing-role job posts at the company (expansion signal)."""
    cfg = config or load_yaml("enrich.yaml")
    jobs_cfg = cfg.get("jobs") or {}
    keywords = jobs_cfg.get("role_keywords") or ["purchasing", "procurement", "buyer"]
    max_results = int(jobs_cfg.get("max_results", 5))
    results, _stats = router.search(
        f'"{company_name}" ({keywords[0]} OR {keywords[1] if len(keywords) > 1 else keywords[0]}) job',
        query_type="general",
        max_results=max_results,
    )
    return [{"title": r.title, "url": r.url, "snippet": r.snippet, "engine": r.engine}
            for r in results]
