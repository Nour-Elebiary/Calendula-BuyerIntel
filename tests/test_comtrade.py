"""Comtrade connector tests (monkeypatched HTTP; no network)."""
from __future__ import annotations

import pytest

from buyerintel.catalog import seed_hs, seed_products
from buyerintel.market import comtrade


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture()
def catalog(session):
    seed_products(session)
    seed_hs(session)
    session.commit()
    return session


def _row(partner, flow, value, qty, customs=None, period="2024", cmd="090961"):
    return {
        "reporterCode": 818,
        "partnerCode": partner,
        "flowCode": flow,
        "cmdCode": cmd,
        "period": period,
        "primaryValue": value,
        "netWgt": qty,
        "customsCode": customs,
    }


def test_dedupe_prefers_general_trade_and_largest_value():
    rows = [
        _row("842", "X", 100.0, 10.0, customs="C10"),
        _row("842", "X", 900.0, 90.0, customs="C00"),
        _row("842", "X", 500.0, 50.0, customs=None),
    ]
    best = comtrade._dedupe_rows(rows)
    assert len(best) == 1
    assert best[("842", "X")]["primaryValue"] == 900.0


def test_fetch_comtrade_upserts_flows(session, catalog, monkeypatch):
    seen_urls = []
    seen_params = []

    def fake_get(url, *, params=None, attempts=3):
        seen_urls.append(url)
        seen_params.append(params)
        payload = {"data": [_row("842", "X", 1000.0, 100.0), _row("156", "X", 2000.0, 200.0)]}
        return FakeResponse(payload)

    sleeps = []
    monkeypatch.setattr(comtrade, "polite_get", fake_get)
    monkeypatch.setattr(comtrade.time, "sleep", lambda s: sleeps.append(s))

    stats = comtrade.fetch_comtrade(
        session, years=[2024], hs_codes=["090961"], flow_codes=["X"]
    )
    session.commit()

    assert stats.calls == 1
    assert stats.flows_created == 2
    assert seen_urls[0].endswith("/preview/C/A/HS")
    # one period per call constraint
    assert seen_params[0]["period"] == "2024"
    assert seen_params[0]["reporterCode"] == "818"
    assert seen_params[0]["cmdCode"] == "090961"

    from sqlalchemy import select

    from buyerintel.db.models import TradeFlow

    flows = session.scalars(select(TradeFlow)).all()
    partners = {f.partner: f.value_usd for f in flows}
    assert partners["USA"] == 1000.0
    assert partners["CHN"] == 2000.0
    first = flows[0]
    assert first.unit_value == pytest.approx(10.0)


def test_fetch_comtrade_call_budget(session, catalog, monkeypatch):
    calls = []

    def fake_get(url, *, params=None, attempts=3):
        calls.append(url)
        return FakeResponse({"data": []})

    monkeypatch.setattr(comtrade, "polite_get", fake_get)
    monkeypatch.setattr(comtrade.time, "sleep", lambda s: None)

    stats = comtrade.fetch_comtrade(
        session, years=[2024, 2025], hs_codes=["090961", "090963"], max_calls=2
    )
    assert stats.calls == 2
    assert len(stats.skipped) == 2  # the remaining (hs, year) pairs
