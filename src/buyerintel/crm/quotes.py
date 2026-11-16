"""Quote tracker (spec section 16).

Calendula's own prices are decided by the user and change often, so there
is NO stored price list. Each quote records date, currency, amount and
incoterm. Trade-data unit values appear ONLY as a reference benchmark -
never as a suggested price.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.db.models import Quote, TradeFlow

INCOTERMS = ["EXW", "FCA", "FOB", "CFR", "CIF", "DAP", "DPU", "DDP"]
STATUSES = ["draft", "sent", "viewed", "accepted", "rejected", "expired"]


def log_quote(session: Session, *, company_id: int, price: float, currency: str = "USD",
              incoterm: str = "FOB", products: list[str] | None = None,
              qty: float | None = None, status: str = "sent",
              date: datetime | None = None) -> Quote:
    """Record one quote. Calendula's own price comes from the user's input."""
    if incoterm not in INCOTERMS:
        raise ValueError(f"unknown incoterm {incoterm!r}; allowed: {INCOTERMS}")
    if status not in STATUSES:
        raise ValueError(f"unknown quote status {status!r}; allowed: {STATUSES}")
    quote = Quote(company_id=company_id, products=products or [], qty=qty,
                  incoterm=incoterm, price=price, currency=currency,
                  status=status, date=date or datetime.now(timezone.utc))
    session.add(quote)
    session.flush()
    return quote


def benchmark_unit_value(session: Session, hs: str, *, partner: str | None = None) -> dict | None:
    """REFERENCE ONLY: latest Egypt-export unit value for the HS (USD/kg).

    Shown next to the quote for the user's judgement - the system never
    suggests a price (Calendula's pricing is the user's call, spec 16).
    """
    query = select(TradeFlow).where(TradeFlow.reporter == "EGY", TradeFlow.hs == hs,
                                    TradeFlow.unit_value.is_not(None))
    if partner:
        query = query.where(TradeFlow.partner == partner)
    row = session.scalars(query.order_by(TradeFlow.period.desc()).limit(1)).first()
    if row is None:
        return None
    return {"hs": row.hs, "partner": row.partner, "period": row.period,
            "unit_value_usd_per_kg": row.unit_value,
            "note": "reference benchmark from trade statistics - not a suggested price"}
