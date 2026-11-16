"""US public BOL connector: fixture-tested parser + graceful degradation."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from buyerintel.db.models import Company
from buyerintel.shipments.base import parse_date, to_float
from buyerintel.shipments.us_bol import ImportYetiPublicSource, parse_company_html

FIXTURE = Path(__file__).parent / "fixtures" / "importyeti_company.html"


def test_parse_company_html_extracts_rows():
    rows = parse_company_html(FIXTURE.read_text(encoding="utf-8"))
    assert len(rows) == 3
    first = rows[0]
    assert first["date"] == "2025-11-02"
    assert first["qty"] == 2500.0
    assert first["value_usd"] == 7125.0
    assert first["hs_code"] == "090300"   # regex pulls the HS code from the description
    assert first["origin"] == "EGY" and first["dest"] == "USA"


def test_parse_company_html_no_invent():
    """Auth wall / empty / JS shell -> [], never fabricated rows."""
    assert parse_company_html("") == []
    assert parse_company_html("   ") == []
    authwall = "<html><body>Please Sign In to view shipments</body></html>"
    assert parse_company_html(authwall) == []
    shell = "<html><body><div id='root'></div></body></html>"
    assert parse_company_html(shell) == []


def test_normalize_drops_unusable_rows(session):
    src = ImportYetiPublicSource(session)
    assert src.normalize({"date": None, "qty": None, "value_usd": None}) is None
    out = src.normalize({"date": "2025-06-18", "qty": "1,200", "value_usd": "3,300.00",
                         "hs_code": "090300", "product_desc": "peppermint",
                         "origin": "EGY", "dest": "USA"})
    assert out is not None
    assert out["date"] == parse_date("2025-06-18")
    assert out["qty"] == 1200.0 and out["value_usd"] == 3300.0


def test_fetch_shipments_uses_slug_and_pacing(session, monkeypatch):
    calls: list[str] = []

    class _Resp:
        status_code = 200
        text = FIXTURE.read_text(encoding="utf-8")

    def fake_get(url, **kwargs):
        calls.append(url)
        return _Resp()

    sleeps: list[float] = []
    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr("buyerintel.shipments.us_bol.time.sleep", sleeps.append)

    company = Company(canonical_name="Giza Herbs Trading Co!")
    session.add(company)
    session.flush()
    src = ImportYetiPublicSource(session)
    rows = src.fetch_shipments(company)
    assert calls == ["https://www.importyeti.com/company/giza-herbs-trading-co"]
    assert sleeps and sleeps[0] == 3.0
    assert len(rows) == 3


def test_fetch_shipments_degrades_on_error_and_status(session, monkeypatch):
    company = Company(canonical_name="Broken Co")
    session.add(company)
    session.flush()
    src = ImportYetiPublicSource(session)

    def boom(url, **kwargs):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", boom)
    monkeypatch.setattr("buyerintel.shipments.us_bol.time.sleep", lambda s: None)
    assert src.fetch_shipments(company) == []

    class _404:
        status_code = 404
        text = "nope"

    monkeypatch.setattr(httpx, "get", lambda url, **k: _404())
    assert src.fetch_shipments(company) == []


def test_disabled_source_fetches_nothing(session, monkeypatch):
    company = Company(canonical_name="Disabled Co")
    session.add(company)
    session.flush()
    src = ImportYetiPublicSource(session, config={"sources": {"us_bol": {"enabled": False}}})
    assert src.enabled() is False

    def fail(url, **kwargs):
        raise AssertionError("network must not be touched when disabled")

    monkeypatch.setattr(httpx, "get", fail)
    assert src.fetch_shipments(company) == []


def test_run_for_company_persists_shipments(session, monkeypatch):
    company = Company(canonical_name="Persist Co")
    session.add(company)
    session.flush()
    src = ImportYetiPublicSource(session)

    class _Resp:
        status_code = 200
        text = FIXTURE.read_text(encoding="utf-8")

    monkeypatch.setattr(httpx, "get", lambda url, **k: _Resp())
    monkeypatch.setattr("buyerintel.shipments.us_bol.time.sleep", lambda s: None)
    stats = src.run_for_company(company)
    assert stats["created"] == 3 and stats["skipped"] == 0
    # second run: idempotent, nothing new
    stats2 = src.run_for_company(company)
    assert stats2["created"] == 0
