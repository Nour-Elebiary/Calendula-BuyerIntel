"""Comext connector tests: JSON-stat decoder + flow upserts (monkeypatched HTTP)."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from buyerintel.catalog import seed_hs, seed_products
from buyerintel.market import comext
from buyerintel.db.models import MarketSignal, TradeFlow


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _cube(products, times, indicators, values):
    """Build a minimal JSON-stat 2.0 cube."""
    ids = ["product", "time", "indicator"]
    size = [len(products), len(times), len(indicators)]
    dimension = {
        dim: {"category": {"index": {code: i for i, code in enumerate(codes)}}}
        for dim, codes in zip(ids, [products, times, indicators])
    }
    return {"id": ids, "size": size, "dimension": dimension, "value": values}


def test_decode_jsonstat_row_major_strides():
    payload = _cube(
        products=["090961", "120740"],
        times=["2023", "2024"],
        indicators=["VALUE_IN_EUROS", "QUANTITY_IN_100KG"],
        values={
            0: 100.0,   # 090961, 2023, VALUE
            3: 55.0,    # 090961, 2024, QTY
            5: 250.0,   # 120740, 2023, QTY
            6: 400.0,   # 120740, 2024, VALUE
        },
    )
    rows = comext.decode_jsonstat(payload)
    by_key = {(r["product"], r["time"], r["indicator"]): r["_value"] for r in rows}
    assert by_key[("090961", "2023", "VALUE_IN_EUROS")] == 100.0
    assert by_key[("090961", "2024", "QUANTITY_IN_100KG")] == 55.0
    assert by_key[("120740", "2023", "QUANTITY_IN_100KG")] == 250.0
    assert by_key[("120740", "2024", "VALUE_IN_EUROS")] == 400.0
    assert len(rows) == 4


def test_fetch_comext_converts_eur_to_usd(session, monkeypatch):
    seed_products(session)
    seed_hs(session)
    session.commit()
    # pre-seed the FX signal the connector reads
    session.add(MarketSignal(source="fx", subject="EUR:USD", period="2026-01-01", value=1.1))
    session.commit()

    seen_urls = []
    seen_params = []

    def fake_get(url, *, params=None, attempts=3):
        seen_urls.append(url)
        seen_params.append(params)
        payload = _cube(
            products=["090961"],
            times=["2023", "2024"],
            indicators=["VALUE_IN_EUROS", "QUANTITY_IN_100KG"],
            values={0: 1000.0, 1: 50.0, 2: 2000.0, 3: 80.0},
        )
        return FakeResponse(payload)

    monkeypatch.setattr(comext, "polite_get", fake_get)
    stats = comext.fetch_comext(session, years=[2023, 2024], hs_codes=["090961"])
    session.commit()

    assert stats.calls == 1
    assert stats.flows_created == 2
    # dedicated comext base url + dataset
    assert "/comext/dissemination/statistics/1.0/data/DS-045409" in seen_urls[0]
    assert seen_params[0]["reporter"] == "EU27_2020"
    assert seen_params[0]["partner"] == "EG"
    assert seen_params[0]["flow"] == "1"
    # repeated time params expanded by httpx from the list value
    assert seen_params[0]["time"] == ["2023", "2024"]

    flows = session.scalars(select(TradeFlow).order_by(TradeFlow.period)).all()
    assert [(f.period, f.value_usd, f.qty) for f in flows] == [
        ("2023", pytest.approx(1100.0), 5000.0),   # 1000 EUR * 1.1, 50 x 100 kg
        ("2024", pytest.approx(2200.0), 8000.0),
    ]
    assert flows[0].reporter == "EU27"
    assert flows[0].partner == "EGY"


def test_fetch_comext_without_fx_stores_none_value(session, monkeypatch):
    seed_products(session)
    seed_hs(session)
    session.commit()

    def fake_get(url, *, params=None, attempts=3):
        payload = _cube(
            products=["120740"],
            times=["2023"],
            indicators=["VALUE_IN_EUROS", "QUANTITY_IN_100KG"],
            values={0: 500.0, 1: 10.0},
        )
        return FakeResponse(payload)

    monkeypatch.setattr(comext, "polite_get", fake_get)
    stats = comext.fetch_comext(session, years=[2023], hs_codes=["120740"])
    session.commit()
    flow = session.scalars(select(TradeFlow)).one()
    assert flow.value_usd is None  # no FX -> value stays None, never a wrong number
    assert flow.qty == 1000.0
    assert stats.flows_created == 1
