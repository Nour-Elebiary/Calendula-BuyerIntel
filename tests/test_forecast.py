"""Forecast math (spec section 15, M7) - the M7 acceptance test:

"Forecast outputs window, quantity range, value range and confidence on
fixture data."
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from buyerintel.db.models import Company, Forecast, Shipment, TradeFlow
from buyerintel.forecast import (
    benchmark_unit_value,
    confidence_label,
    forecast_all,
    forecast_company,
    seasonality_index,
)

NOW = datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC: matches SQLite DateTime columns


def _seed_company(session, name="Forecast Co"):
    company = Company(canonical_name=name, tier=1)
    session.add(company)
    session.flush()
    return company


def _add_shipment(session, company, days_ago, qty=None, value_usd=None, hs="0903", unit="KG"):
    session.add(Shipment(source="test", company_id=company.id,
                         date=NOW - timedelta(days=days_ago), hs_code=hs,
                         qty=qty, unit=unit, value_usd=value_usd))
    session.flush()


CFG = {"forecast": {"window_factor": 0.25, "min_interval_days": 15,
                    "max_interval_days": 365, "seasonality": True,
                    "default_unit_value_usd": 2.5,
                    "confidence": {"low_below": 3, "high_from": 5}},
       "scoring": {"shipment_recency_days": 180}}


# ----------------------------------------------------------- acceptance core
def test_forecast_outputs_all_four_fields(session):
    company = _seed_company(session)
    # 5 dated shipments ~ every 100 days -> medium-high confidence
    for days_ago, qty, value in [(400, 1000, 2500), (300, 1500, 3750),
                                 (200, 1200, 3000), (100, 1800, 4500),
                                 (20, 1400, 3500)]:
        _add_shipment(session, company, days_ago, qty, value)
    rows = forecast_company(session, company, config=CFG)
    session.flush()
    assert len(rows) == 1
    f = rows[0]
    # the four acceptance fields, all populated
    assert f.window_start is not None and f.window_end is not None
    assert f.window_start < f.window_end
    assert f.window_start > NOW - timedelta(days=1)   # window is in the FUTURE
    assert f.qty_min == 1000.0 and f.qty_max == 1800.0
    assert f.value_min_usd is not None and f.value_max_usd is not None
    assert f.value_min_usd < f.value_max_usd
    assert f.confidence in ("low", "medium", "high")
    assert f.n_shipments == 5
    # window math: last + adjusted interval +/- 25%
    assert f.last_shipment == NOW - timedelta(days=20)
    assert f.median_interval_days == pytest.approx(100, abs=3)


def test_confidence_low_below_three_shipments(session):
    company = _seed_company(session)
    _add_shipment(session, company, 100, 500, 1250)
    _add_shipment(session, company, 200, 600, 1500)
    rows = forecast_company(session, company, config=CFG)
    assert rows[0].confidence == "low"      # spec: fewer than 3 -> LOW
    assert rows[0].n_shipments == 2
    # everywhere = the label is persisted and readable from the table
    stored = session.scalar(select(Forecast))
    assert stored.confidence == "low"


def test_confidence_bands(session):
    cfg = CFG["forecast"]
    assert confidence_label(0, CFG) == "low"
    assert confidence_label(2, CFG) == "low"
    assert confidence_label(3, CFG) == "medium"
    assert confidence_label(4, CFG) == "medium"
    assert confidence_label(5, CFG) == "high"


def test_single_shipment_no_forecast(session):
    company = _seed_company(session)
    _add_shipment(session, company, 50, 500, 1250)
    assert forecast_company(session, company, config=CFG) == []
    assert session.query(Forecast).count() == 0


# ----------------------------------------------------------- seasonality
def test_seasonality_index_from_trade_flow(session):
    lead_month = (NOW.month % 12) + 1
    period = f"2025-{lead_month:02d}"
    session.add(TradeFlow(reporter="EGY", partner="USA", hs="0903", period=period,
                          value_usd=300.0))
    session.add(TradeFlow(reporter="EGY", partner="USA", hs="0903", period="2025-02",
                          value_usd=100.0))
    session.flush()
    index, month = seasonality_index(session, "0903", dest="USA")
    assert month == lead_month
    assert index == 1.5          # lead month 300 vs the 200 overall monthly average
    # no data -> (1.0, 0), no adjustment
    assert seasonality_index(session, "9999") == (1.0, 0)


def test_seasonality_pulls_window_earlier(session):
    company = _seed_company(session)
    lead_month = (NOW.month % 12) + 1
    session.add(TradeFlow(reporter="EGY", partner="USA", hs="0903",
                          period=f"2025-{lead_month:02d}", value_usd=300.0))
    session.add(TradeFlow(reporter="EGY", partner="USA", hs="0903",
                          period="2025-02", value_usd=100.0))
    session.flush()
    # shipments exactly 100 days apart, no seasonality
    cfg_no = {**CFG, "forecast": {**CFG["forecast"], "seasonality": False}}
    _add_shipment(session, company, 200, 1000, 2500)
    _add_shipment(session, company, 100, 1000, 2500)
    baseline = forecast_company(session, company, config=cfg_no)[0]
    raw_days = baseline.detail["raw_median_days"]
    baseline_end = baseline.window_end   # the same row is upserted in place

    # with seasonality the high-demand lead month shortens the expected interval
    adjusted = forecast_company(session, company, config=CFG)[0]
    assert adjusted.seasonality_index > 1.0
    assert adjusted.detail["adjusted_days"] < raw_days
    assert adjusted.window_end < baseline_end   # earlier window


# ----------------------------------------------------------- unit value
def test_unit_value_source_order(session):
    company = _seed_company(session)
    s = [Shipment(source="test", company_id=company.id, date=NOW, hs_code="0903",
                  qty=100.0, value_usd=300.0),
         Shipment(source="test", company_id=company.id, date=NOW, hs_code="0903",
                  qty=200.0, value_usd=800.0)]  # ratios 3.0 and 4.0 -> median 3.5
    session.add_all(s)
    session.flush()
    uv, src = benchmark_unit_value(session, company, "0903", s, CFG)
    assert uv == 3.5 and src == "shipments"

    # no own quantities -> latest Egypt trade-flow unit value for the HS
    s2 = [Shipment(source="test", company_id=company.id, date=NOW, hs_code="1211",
                   qty=None, value_usd=None)]
    session.add(TradeFlow(reporter="EGY", partner="WLD", hs="1211", period="2024",
                          unit_value=1.75))
    session.flush()
    uv2, src2 = benchmark_unit_value(session, company, "1211", s2, CFG)
    assert uv2 == 1.75 and src2 == "trade_flow"

    # neither -> config default
    s3 = [Shipment(source="test", company_id=company.id, date=NOW, hs_code="9999")]
    uv3, src3 = benchmark_unit_value(session, company, "9999", s3, CFG)
    assert uv3 == 2.5 and src3 == "default"


def test_value_range_uses_unit_value(session):
    company = _seed_company(session)
    _add_shipment(session, company, 100, 1000, 3000)   # uv = 3.0
    _add_shipment(session, company, 200, 2000, 6000)
    f = forecast_company(session, company, config=CFG)[0]
    assert f.uv_source == "shipments" and f.unit_value_usd == 3.0
    assert f.value_min_usd == 3000.0 and f.value_max_usd == 6000.0


# ----------------------------------------------------------- upserts/groups
def test_forecast_upsert_unique_per_product_key(session):
    """Recomputing never duplicates: one row per (company, product_key)."""
    company = _seed_company(session)
    _add_shipment(session, company, 100, 1000, 3000, hs="0903")
    _add_shipment(session, company, 200, 1100, 3300, hs="0903")
    _add_shipment(session, company, 300, 500, 1000, hs="1211")   # no qty->? has qty
    _add_shipment(session, company, 400, 600, 1200, hs="1211")
    first = forecast_company(session, company, config=CFG)
    assert {f.product_key for f in first} == {"0903", "1211"}
    second = forecast_company(session, company, config=CFG)
    assert len(second) == 2
    assert session.query(Forecast).count() == 2   # upsert, not insert


def test_rows_without_hs_group_under_all(session):
    company = _seed_company(session)
    for days_ago in (100, 200):
        session.add(Shipment(source="test", company_id=company.id,
                             date=NOW - timedelta(days=days_ago), hs_code=None,
                             qty=700.0, value_usd=1750.0))
    session.flush()
    f = forecast_company(session, company, config=CFG)[0]
    assert f.product_key == "all"


def test_forecast_all_scopes_and_skips(session):
    company = _seed_company(session)
    other = _seed_company(session, "No Shipments Co")
    for days_ago in (100, 200):
        _add_shipment(session, company, days_ago, 800, 2000)
    rows = forecast_all(session, config=CFG, company_id=other.id)
    assert rows == []
    rows = forecast_all(session, config=CFG)
    assert [r.company_id for r in rows] == [company.id]


# interval clamping sanity
def test_interval_clamped_to_config_bounds(session):
    company = _seed_company(session)
    _add_shipment(session, company, 400)   # 400-day gap -> clamped to 365
    _add_shipment(session, company, 2)
    f = forecast_company(session, company, config=CFG)[0]
    assert f.median_interval_days == 365.0
