"""Forecasting per importer and product (spec section 15, M7).

From the company's `shipment` rows, per product (HS code; rows without one
group under 'all'):

  - last shipment date, median interval between shipments
  - expected next order window = last shipment + median interval,
    ADJUSTED for seasonality: the seasonality index s of the expected
    month scales the interval (s > 1 = high-demand month pulls orders
    earlier: interval / s), then the window is +/- window_factor x interval
  - quantity range = min/max of shipment quantities (kg)
  - estimated value range = quantity range x benchmark unit value (USD)
    Unit value source order: the company's own shipments (median of
    value/qty), else the latest Egypt-export trade_flow unit value for the
    HS + destination (stored in USD at ingestion), else the config default.
  - confidence: low < 3 shipments, medium 3-4, high >= 5 (config) - shown
    everywhere per the spec.

Forecasting ACTIVATES per country when shipment coverage passes the config
threshold; below it the proxies (trade statistics, expo activity, job
posts) carry the signal - the scoring components already degrade.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.config import load_yaml
from buyerintel.db.models import Company, Forecast, Shipment, TradeFlow


def seasonality_index(session: Session, hs: str, *, dest: str | None = None) -> tuple[float, int]:
    """(index for the EXPECTED month, month) from monthly Egypt-export flows.

    Index = that month's average value / the overall monthly average.
    Returns (1.0, 0) when no monthly market data exists - no adjustment.
    """
    query = select(TradeFlow.period, TradeFlow.value_usd).where(
        TradeFlow.reporter == "EGY", TradeFlow.hs == hs,
        TradeFlow.period.like("%-%"))
    if dest:
        query = query.where(TradeFlow.partner == dest)
    rows = session.execute(query).all()
    if not rows:
        return 1.0, 0
    by_month: dict[int, list[float]] = {}
    for period, value in rows:
        if value is None or "-" not in period:
            continue
        try:
            by_month.setdefault(int(period.split("-")[1]), []).append(float(value))
        except (ValueError, IndexError):
            continue
    if not by_month:
        return 1.0, 0
    averages = {m: statistics.fmean(v) for m, v in by_month.items()}
    overall = statistics.fmean(list(averages.values()))
    if overall <= 0:
        return 1.0, 0
    # the month the expected order would land in (one lead month ahead)
    lead = (datetime.now(timezone.utc).month % 12) + 1
    return round(averages.get(lead, overall) / overall, 3), lead


def benchmark_unit_value(session: Session, company: Company, hs: str | None,
                         shipments: list[Shipment], cfg: dict) -> tuple[float | None, str]:
    """(unit value USD, source) - own shipments, then trade flows, then default."""
    ratios = [s.value_usd / s.qty for s in shipments
              if s.value_usd and s.qty and s.qty > 0]
    if ratios:
        return round(statistics.median(ratios), 4), "shipments"
    if hs:
        row = session.execute(
            select(TradeFlow.unit_value, TradeFlow.period)
            .where(TradeFlow.reporter == "EGY", TradeFlow.hs == hs)
            .order_by(TradeFlow.period.desc()).limit(1)
        ).first()
        if row and row[0]:
            return round(float(row[0]), 4), "trade_flow"
    default = (cfg.get("forecast") or {}).get("default_unit_value_usd")
    return (float(default), "default") if default else (None, "default")


def confidence_label(n_shipments: int, cfg: dict) -> str:
    fc = cfg.get("forecast") or {}
    if n_shipments >= int(fc.get("high_from", 5)):
        return "high"
    if n_shipments >= int(fc.get("low_below", 3)):
        return "medium"
    return "low"


def forecast_company(session: Session, company: Company, *,
                     config: dict | None = None) -> list[Forecast]:
    """Compute + upsert forecasts for one company; returns the fresh rows."""
    cfg = config or load_yaml("shipments.yaml")
    fc = cfg.get("forecast") or {}
    window_factor = float(fc.get("window_factor", 0.25))
    min_interval = float(fc.get("min_interval_days", 15))
    max_interval = float(fc.get("max_interval_days", 365))

    shipments = session.scalars(
        select(Shipment).where(Shipment.company_id == company.id,
                               Shipment.date.is_not(None))
        .order_by(Shipment.date)
    ).all()
    groups: dict[str, list[Shipment]] = {}
    for s in shipments:
        groups.setdefault(s.hs_code or "all", []).append(s)

    out: list[Forecast] = []
    for product_key, rows in groups.items():
        dates = [r.date for r in rows if r.date is not None]
        if len(dates) < 2:
            continue  # a forecast needs at least an interval (2 shipments)
        intervals = [(b - a).days for a, b in zip(dates, dates[1:])]
        median_days = float(statistics.median(intervals))
        median_days = min(max_interval, max(min_interval, median_days))
        last = dates[-1]

        index, lead_month = (1.0, 0)
        if fc.get("seasonality", True):
            index, lead_month = seasonality_index(session, product_key,
                                                  dest=rows[-1].dest)
        adjusted_days = median_days / index if index > 0 else median_days
        adjusted_days = min(max_interval, max(min_interval, adjusted_days))
        expected = last + timedelta(days=adjusted_days)
        half_window = timedelta(days=window_factor * adjusted_days)
        window_start = expected - half_window
        window_end = expected + half_window

        quantities = [r.qty for r in rows if r.qty]
        qty_min = min(quantities) if quantities else None
        qty_max = max(quantities) if quantities else None
        unit_value, uv_source = benchmark_unit_value(session, company, product_key
                                                     if product_key != "all" else None,
                                                     rows, cfg)
        value_min = round(qty_min * unit_value, 2) if (qty_min and unit_value) else None
        value_max = round(qty_max * unit_value, 2) if (qty_max and unit_value) else None
        n = len(rows)
        confidence = confidence_label(n, cfg)

        existing = session.scalar(
            select(Forecast).where(Forecast.company_id == company.id,
                                   Forecast.product_key == product_key))
        if existing is None:
            existing = Forecast(company_id=company.id, product_key=product_key)
            session.add(existing)
        existing.n_shipments = n
        existing.last_shipment = last
        existing.median_interval_days = round(median_days, 1)
        existing.window_start = window_start
        existing.window_end = window_end
        existing.qty_min = qty_min
        existing.qty_max = qty_max
        existing.unit_value_usd = unit_value
        existing.uv_source = uv_source
        existing.value_min_usd = value_min
        existing.value_max_usd = value_max
        existing.seasonality_index = index
        existing.confidence = confidence
        existing.detail = {"adjusted_days": round(adjusted_days, 1),
                           "lead_month": lead_month,
                           "raw_median_days": round(float(statistics.median(intervals)), 1)}
        existing.computed_at = datetime.now(timezone.utc)
        session.flush()
        out.append(existing)
    return out


def forecast_all(session: Session, *, config: dict | None = None,
                 company_id: int | None = None) -> list[Forecast]:
    """Forecast every company with >= 2 dated shipments (or one company)."""
    query = select(Company).order_by(Company.id)
    if company_id is not None:
        query = query.where(Company.id == company_id)
    companies = session.scalars(query).all()
    out: list[Forecast] = []
    for company in companies:
        out.extend(forecast_company(session, company, config=config))
    return out
