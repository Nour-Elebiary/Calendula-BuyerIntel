"""Market report queries backing the Market Intelligence UI page (spec section 16).

Read-only helpers over trade_flow and market_signal: Egypt exports by partner,
destination imports per HS, unit values, and the demand-signal digest.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from buyerintel.db.models import HsCandidate, MarketSignal, Product, TradeFlow


@dataclass
class FlowRow:
    period: str
    hs: str
    partner: str
    value_usd: float | None
    qty: float | None
    unit_value: float | None
    reporter: str | None = None


def exports_by_partner(
    session: Session,
    *,
    reporter: str = "EGY",
    hs: str | None = None,
    year: str | None = None,
) -> list[FlowRow]:
    """Egypt export flows (optionally filtered by HS and year), largest first."""
    query = (
        select(TradeFlow)
        .where(TradeFlow.reporter == reporter)
        .order_by(TradeFlow.period.desc(), TradeFlow.value_usd.desc().nulls_last())
    )
    if hs:
        query = query.where(TradeFlow.hs == hs)
    if year:
        query = query.where(TradeFlow.period.startswith(year))
    return [
        FlowRow(row.period, row.hs, row.partner, row.value_usd, row.qty, row.unit_value, row.reporter)
        for row in session.scalars(query).all()
    ]


def imports_by_year(
    session: Session,
    *,
    reporter: str = "EU27",
    partner: str = "EGY",
    hs: str | None = None,
) -> list[FlowRow]:
    """Destination imports per HS by year (default: EU27 imports from Egypt)."""
    query = select(TradeFlow).where(TradeFlow.reporter == reporter, TradeFlow.partner == partner)
    if hs:
        query = query.where(TradeFlow.hs == hs)
    rows = session.scalars(query.order_by(TradeFlow.period.desc(), TradeFlow.hs)).all()
    return [
        FlowRow(row.period, row.hs, row.partner, row.value_usd, row.qty, row.unit_value, row.reporter)
        for row in rows
    ]


def unit_values(
    session: Session,
    *,
    reporter: str = "EGY",
    year: str | None = None,
) -> list[tuple[str, str, str, float | None]]:
    """(period, hs, partner, USD/kg) where both value and qty exist."""
    query = select(TradeFlow).where(
        TradeFlow.reporter == reporter,
        TradeFlow.value_usd.is_not(None),
        TradeFlow.qty.is_not(None),
        TradeFlow.qty > 0,
    )
    if year:
        query = query.where(TradeFlow.period.startswith(year))
    result: list[tuple[str, str, str, float | None]] = []
    for row in session.scalars(query).all():
        unit_value = row.unit_value or (row.value_usd / row.qty if row.qty else None)
        result.append((row.period, row.hs, row.partner, round(unit_value, 4) if unit_value else None))
    result.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    return result


def hs_products(session: Session) -> list[tuple[str, list[str]]]:
    """HS code -> product names using that code (for labels in the UI)."""
    mapping: dict[str, list[str]] = {}
    rows = (
        session.execute(
            select(HsCandidate.hs_code, Product.name)
            .join(Product, Product.id == HsCandidate.product_id)
            .order_by(HsCandidate.hs_code, Product.name)
        )
        .all()
    )
    for code, name in rows:
        mapping.setdefault(code, []).append(name)
    return sorted(mapping.items())


def market_digest(session: Session) -> dict:
    """Small digest for the dashboard: totals per source and latest FX."""
    trade_count = session.scalar(select(func.count(TradeFlow.id))) or 0
    hs_count = session.scalar(
        select(func.count(func.distinct(TradeFlow.hs)))
    ) or 0
    signals = session.execute(
        select(MarketSignal.source, func.count(MarketSignal.id)).group_by(MarketSignal.source)
    ).all()
    latest_fx = session.scalar(
        select(MarketSignal.value)
        .where(MarketSignal.source == "fx", MarketSignal.subject == "EUR:USD")
        .order_by(MarketSignal.period.desc(), MarketSignal.id.desc())
        .limit(1)
    )
    return {
        "trade_rows": int(trade_count),
        "hs_codes": int(hs_count),
        "signals": {source: int(n) for source, n in signals},
        "latest_eur_usd": latest_fx,
    }
