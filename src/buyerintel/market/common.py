"""Shared helpers for market connectors: primary HS codes, polite HTTP, storage."""
from __future__ import annotations

import random
import time
from typing import Iterable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.config import load_yaml
from buyerintel.db.models import HsCandidate, MarketSignal, TradeFlow


def primary_hs_codes(session: Session, *, primary_only: bool = False) -> list[str]:
    """Distinct HS codes configured for the catalog (flagged ones included)."""
    query = select(HsCandidate.hs_code).distinct()
    if primary_only:
        query = query.where(HsCandidate.is_primary.is_(True))
    return sorted(code for code in session.scalars(query).all() if code)


def egress_mode() -> str:
    """Current egress mode from network.yaml (direct by default)."""
    return (load_yaml("network.yaml").get("mode") or "direct").lower()


def polite_get(url: str, *, params: dict | None = None, attempts: int = 3) -> httpx.Response:
    """GET with retries, exponential backoff + jitter, and a clear user agent.

    Raises on the final failure. Timeout from network.yaml (default 30s).
    """
    network = load_yaml("network.yaml")
    timeout = float((network.get("direct") or {}).get("timeout_seconds", 30))
    mode = egress_mode()
    if mode not in {"direct", "proxy", "tor"}:
        mode = "direct"
    if mode == "tor":
        raise RuntimeError(
            "egress mode 'tor' is enabled but tor routing is not wired in this module; "
            "targeted fallback goes through a dedicated adapter per site"
        )
    headers = {"User-Agent": "Calendula-BuyerIntel/0.1 (local personal research)"}
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = httpx.get(url, params=params, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response
        except (httpx.HTTPError, httpx.HTTPStatusError) as exc:  # noqa: PERF203
            last_error = exc
            time.sleep(min(2**attempt + random.uniform(0, 1), 15))
    raise last_error if last_error else RuntimeError("polite_get failed")


def upsert_trade_flow(
    session: Session,
    *,
    reporter: str,
    partner: str,
    hs: str,
    period: str,
    value_usd: float | None,
    qty: float | None = None,
    source: str = "market",
) -> bool:
    """Insert a trade flow row unless it already exists (idempotent). Returns created."""
    session.info["audit_source"] = source
    existing = session.scalar(
        select(TradeFlow).where(
            TradeFlow.reporter == reporter,
            TradeFlow.partner == partner,
            TradeFlow.hs == hs,
            TradeFlow.period == period,
        )
    )
    if existing is not None:
        return False
    unit_value = None
    if value_usd and qty:
        unit_value = round(value_usd / qty, 3)
    session.add(
        TradeFlow(
            reporter=reporter,
            partner=partner,
            hs=hs,
            period=period,
            value_usd=value_usd,
            qty=qty,
            unit_value=unit_value,
        )
    )
    return True


def upsert_market_signal(
    session: Session,
    *,
    source: str,
    subject: str,
    period: str,
    value: float | None,
    raw_json: dict | None = None,
) -> bool:
    """Insert a market signal unless it already exists (idempotent). Returns created."""
    session.info["audit_source"] = f"signal:{source}"
    existing = session.scalar(
        select(MarketSignal).where(
            MarketSignal.source == source,
            MarketSignal.subject == subject,
            MarketSignal.period == period,
        )
    )
    if existing is not None:
        return False
    session.add(
        MarketSignal(
            source=source,
            subject=subject,
            period=period,
            value=value,
            raw_json=raw_json,
        )
    )
    return True


def hs_to_iso(partner_map: dict, partner_code: str) -> str | None:
    """Map a UN numeric partner code to its ISO alpha-3 (or None when unknown)."""
    return partner_map.get(str(partner_code))
