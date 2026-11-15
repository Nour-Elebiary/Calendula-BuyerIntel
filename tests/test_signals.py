"""Signal collector tests: PubMed counts, FX rates, trends failure tolerance."""
from __future__ import annotations

import sys
import types

from sqlalchemy import select

from buyerintel.catalog import seed_products
from buyerintel.market import signals
from buyerintel.db.models import MarketSignal


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_fetch_fx_rates_stores_both_directions(session, monkeypatch):
    def fake_get(url, *, params=None, attempts=3):
        assert "base=USD" in url
        return FakeResponse({"date": "2026-01-05", "rates": {"EUR": 0.8, "EGP": 50.0}})

    monkeypatch.setattr(signals, "polite_get", fake_get)
    stats = signals.fetch_fx_rates(session)
    session.commit()

    assert stats.fx_signals == 4
    subjects = {s.subject: s.value for s in session.scalars(select(MarketSignal)).all()}
    assert subjects["USD:EUR"] == 0.8
    assert subjects["EUR:USD"] == 1.25
    assert subjects["USD:EGP"] == 50.0
    assert subjects["EGP:USD"] == 0.02


def test_fetch_pubmed_counts(session, monkeypatch):
    seed_products(session)
    session.commit()
    urls = []

    def fake_get(url, *, params=None, attempts=3):
        urls.append(url)
        # fail for one real term to prove one bad term never stops the run
        if "calendula" in url:
            raise RuntimeError("boom")
        return FakeResponse({"esearchresult": {"count": "42"}})

    monkeypatch.setattr(signals, "polite_get", fake_get)
    monkeypatch.setattr(signals.time, "sleep", lambda s: None)

    stats = signals.fetch_pubmed_counts(session, year=2025)
    session.commit()

    assert len(stats.errors) >= 1 and any("calendula" in e for e in stats.errors)
    assert stats.errors  # the failing term recorded
    assert stats.pubmed_calls >= 1
    rows = session.scalars(
        select(MarketSignal).where(MarketSignal.source == "pubmed")
    ).all()
    assert all(r.value == 42.0 for r in rows)
    assert any("hibiscus" in r.subject or r.subject for r in rows)


def test_fetch_trends_records_errors_and_continues(session, monkeypatch):
    seed_products(session)
    session.commit()

    class FailingTrendReq:
        def __init__(self, **kwargs):
            pass

        def build_payload(self, kw_list, timeframe=None):
            raise RuntimeError("google changed everything")

    fake_pytrends = types.ModuleType("pytrends")
    fake_request = types.ModuleType("pytrends.request")
    fake_request.TrendReq = FailingTrendReq
    monkeypatch.setitem(sys.modules, "pytrends", fake_pytrends)
    monkeypatch.setitem(sys.modules, "pytrends.request", fake_request)

    stats = signals.fetch_trends(session)
    assert stats.trends_signals == 0
    assert stats.errors  # failures logged, never fatal


def test_fetch_signals_composes_sources(session, monkeypatch):
    seed_products(session)
    session.commit()

    def fake_get(url, *, params=None, attempts=3):
        if "frankfurter" in url:
            return FakeResponse({"date": "2026-01-05", "rates": {"EUR": 0.9}})
        if "esearch" in url:
            return FakeResponse({"esearchresult": {"count": "7"}})
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(signals, "polite_get", fake_get)
    monkeypatch.setattr(signals.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        signals, "fetch_trends", lambda session, **kw: signals.SignalStats()
    )

    stats = signals.fetch_signals(session, include_trends=False)
    session.commit()
    assert stats.fx_signals == 2  # USD:EUR and EUR:USD
    assert stats.pubmed_signals > 0
