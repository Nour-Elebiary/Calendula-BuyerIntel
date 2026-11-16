"""Scheduler task registry (spec section 17).

Each task is a dotted-path importable callable ``task(session, **params)``
returning a short summary string. Failures NEVER crash the scheduler: the
runner catches, records JobRun(status=error) and honors the retry count.
"""
from __future__ import annotations

from typing import Callable

from sqlalchemy.orm import Session

TASKS: dict[str, Callable[..., str]] = {}


def register_task(name: str, fn: Callable[..., str] | None = None) -> Callable:
    """Register (or replace) a task by name; usable as a decorator."""
    if fn is not None:
        TASKS[name] = fn
        return fn

    def deco(inner: Callable[..., str]) -> Callable[..., str]:
        TASKS[name] = inner
        return inner

    return deco


def _safe(label: str, fn: Callable[[], object]) -> str:
    try:
        result = fn()
        return f"{label}: {result}"
    except Exception as exc:  # noqa: BLE001 - a degraded source is not fatal
        return f"{label}: DEGRADED ({str(exc)[:120]})"


# ------------------------------------------------------- the typical jobs
@register_task("market_refresh")
def market_refresh(session: Session, **params) -> str:
    """Comtrade + Comext + signals + FX refresh (spec 17 typical jobs)."""
    from buyerintel.market import comext, comtrade, signals

    parts = [
        _safe("comtrade", lambda: str(comtrade.fetch_comtrade(session))),
        _safe("comext", lambda: str(comext.fetch_comext(session))),
        _safe("signals", lambda: str(signals.fetch_signals(session, include_trends=True))),
        _safe("fx", lambda: str(signals.fetch_fx_rates(session))),
    ]
    session.commit()
    return " | ".join(parts)


@register_task("enrich_queue")
def enrich_queue(session: Session, **params) -> str:
    """Run the resumable S0-S7 enrichment queue."""
    from buyerintel.enrich.engine import EnrichEngine

    stats = EnrichEngine(session).run(limit=params.get("limit"))
    return stats.summary()


@register_task("dedupe_sweep")
def dedupe_sweep(session: Session, **params) -> str:
    from buyerintel.resolve.dedupe import sweep

    result = sweep(session)
    session.commit()
    return str(result)


@register_task("score_recompute")
def score_recompute(session: Session, **params) -> str:
    """Re-score every active company (tiers, weights)."""
    from sqlalchemy import select

    from buyerintel.db.models import Company
    from buyerintel.score.scoring import score_company

    companies = session.scalars(
        select(Company).where(Company.state == "active")
    ).all()
    for company in companies:
        score_company(session, company)
    session.commit()
    return f"scored={len(companies)}"


@register_task("forecast_refresh")
def forecast_refresh(session: Session, **params) -> str:
    from buyerintel.forecast import forecast_all

    rows = forecast_all(session)
    session.commit()
    return f"forecasts={len(rows)}"


@register_task("content_suggestions")
def content_suggestions(session: Session, **params) -> str:
    """Draft posts for trending herbs + refresh the send/skip suggestions."""
    from buyerintel.crm import content as content_mod

    drafts = content_mod.draft_for_trends(session)
    session.commit()
    items = session.scalars(select_content()).all()
    n_sug = 0
    for item in items:
        n_sug += len(content_mod.suggest_recipients(session, item))
    session.commit()
    return f"new_drafts={len(drafts)} suggestions={n_sug}"


def select_content():
    from sqlalchemy import select

    from buyerintel.db.models import ContentItem

    return select(ContentItem)


@register_task("archive_sweep")
def archive_sweep(session: Session, **params) -> str:
    """Archive.org snapshots for companies missing a recent one."""
    from datetime import datetime, timedelta

    from sqlalchemy import select

    from buyerintel.db.models import ArchiveSnapshot, Company
    from buyerintel.enrich.osint import store_archive

    cutoff = datetime.now() - timedelta(days=180)
    done = skipped = 0
    for company in session.scalars(select(Company)).all():
        if not company.domain:
            continue
        latest = session.scalar(
            select(ArchiveSnapshot.timestamp).where(ArchiveSnapshot.url.like(f"%{company.domain}%"))
            .order_by(ArchiveSnapshot.timestamp.desc()).limit(1))
        if latest is not None and latest.replace(tzinfo=None) > cutoff:
            skipped += 1
            continue
        _safe(f"{company.canonical_name}", lambda: store_archive(company.domain))
        done += 1
        session.commit()
    return f"swept={done} fresh={skipped}"


@register_task("expo_refresh")
def expo_refresh(session: Session, **params) -> str:
    """Re-ingest expo lead workbooks from config/expo_maps (no-op if unchanged)."""
    from pathlib import Path

    from buyerintel.connectors.expos import ingest_expo
    from buyerintel.config import CONFIG_DIR

    ingested = 0
    for path in sorted((CONFIG_DIR / "expo_maps").glob("*.*")):
        if path.suffix.lower() not in (".csv", ".xlsx", ".xls"):
            continue
        _safe(path.name, lambda: str(ingest_expo(session, path)))
        ingested += 1
    return f"expo_files={ingested}"
