"""Market report query tests (exports, imports, unit values, digest)."""
from __future__ import annotations

import pytest

from buyerintel.market import report
from buyerintel.db.models import MarketSignal, TradeFlow


@pytest.fixture()
def seeded_flows(session):
    rows = [
        # Egypt exports
        ("EGY", "USA", "090961", "2024", 5000.0, 1000.0),
        ("EGY", "USA", "090961", "2023", 4000.0, 1000.0),
        ("EGY", "DEU", "120740", "2024", 3000.0, 750.0),
        ("EGY", "CHN", "120740", "2024", 2000.0, None),   # no qty -> no unit value
        # EU27 mirror imports from Egypt
        ("EU27", "EGY", "090961", "2024", 4800.0, 960.0),
        ("EU27", "EGY", "090961", "2023", 3900.0, 900.0),
    ]
    for reporter, partner, hs, period, value, qty in rows:
        session.add(TradeFlow(reporter=reporter, partner=partner, hs=hs, period=period,
                              value_usd=value, qty=qty))
    session.commit()
    return session


def test_exports_by_partner_filters_and_sorts(seeded_flows):
    rows = report.exports_by_partner(seeded_flows, year="2024")
    assert len(rows) == 3
    assert rows[0].value_usd == 5000.0  # ordered by value desc
    hs_rows = report.exports_by_partner(seeded_flows, hs="090961")
    assert {r.partner for r in hs_rows} == {"USA"}


def test_imports_by_year_default_eu27_mirror(seeded_flows):
    rows = report.imports_by_year(seeded_flows)
    assert len(rows) == 2
    assert all(r.reporter == "EU27" and r.partner == "EGY" for r in rows)
    one = report.imports_by_year(seeded_flows, hs="120740")
    assert one == []  # only 090961 has EU27 mirror rows


def test_unit_values_skips_rows_without_qty(seeded_flows):
    uv = report.unit_values(seeded_flows, year="2024")
    partners = {p: v for (_period, _hs, p, v) in uv}
    assert partners["USA"] == pytest.approx(5.0)     # 5000 / 1000
    assert partners["DEU"] == pytest.approx(4.0)     # 3000 / 750
    assert "CHN" not in partners                     # qty missing


def test_market_digest_counts_and_fx(session):
    session.add(MarketSignal(source="fx", subject="EUR:USD", period="2026-01-05", value=1.08))
    session.add(MarketSignal(source="trends", subject="karkade", period="2026-10", value=55.0))
    session.add(TradeFlow(reporter="EGY", partner="USA", hs="090961", period="2024",
                          value_usd=5000.0, qty=1000.0))
    session.commit()
    digest = report.market_digest(session)
    assert digest["trade_rows"] == 1
    assert digest["hs_codes"] == 1
    assert digest["signals"] == {"fx": 1, "trends": 1}
    assert digest["latest_eur_usd"] == 1.08


def test_hs_products_mapping(session):
    from buyerintel.catalog import seed_hs, seed_products

    seed_products(session)
    seed_hs(session)
    session.commit()
    mapping = dict(report.hs_products(session))
    assert "120740" in mapping
    assert "sesame" in mapping["120740"]
