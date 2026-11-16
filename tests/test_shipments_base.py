"""ShipmentSource interface + shared helpers (spec section 15, M7)."""
from __future__ import annotations

from datetime import datetime

import pytest

from buyerintel.db.models import Company, Shipment
from buyerintel.shipments.base import (
    ShipmentSource,
    parse_date,
    to_float,
    upsert_shipment,
)


class _FakeSource(ShipmentSource):
    """Contract test double: two rows, one unusable."""

    name = "fake"
    rows = [
        {"date": "2025-03-01", "hs_code": "0903", "qty": "100", "value_usd": "250"},
        {"date": "not a date", "qty": None, "value_usd": None},  # normalize drops
        {"date": "2025-03-01", "hs_code": "0903", "qty": "100", "value_usd": "250"},
    ]

    def list_companies(self):
        return [{"name": "Fake Co", "country": "US"}]

    def fetch_shipments(self, company):
        return self.rows

    def normalize(self, row):
        date = parse_date(row.get("date"))
        if date is None and to_float(row.get("qty")) is None:
            return None
        return {"date": date, "hs_code": row.get("hs_code"),
                "qty": to_float(row.get("qty")), "value_usd": to_float(row.get("value_usd"))}


def test_parse_date_layouts():
    assert parse_date("2025-03-01") == datetime(2025, 3, 1)
    assert parse_date("03/01/2025") == datetime(2025, 3, 1)   # %m/%d/%Y
    assert parse_date("Mar 1, 2025") == datetime(2025, 3, 1)
    assert parse_date("01 Mar 2025") == datetime(2025, 3, 1)
    assert parse_date("") is None and parse_date(None) is None
    assert parse_date("garbage") is None


def test_to_float_tolerant():
    assert to_float("1,234.5") == 1234.5
    assert to_float("1.234,5") == 1234.5        # European decimal comma
    assert to_float("1 234,5") == 1234.5
    assert to_float(12) == 12.0
    assert to_float("") is None and to_float(None) is None
    assert to_float("n/a") is None


def test_upsert_shipment_idempotent(session):
    company = Company(canonical_name="Upsert Co")
    session.add(company)
    session.flush()
    kwargs = {"date": datetime(2025, 3, 1), "hs_code": "0903",
              "qty": 100.0, "value_usd": 250.0}
    assert upsert_shipment(session, source="test", company_id=company.id, **kwargs) is True
    assert upsert_shipment(session, source="test", company_id=company.id, **kwargs) is False
    # a DIFFERENT date/qty/value is a different bill of lading line
    assert upsert_shipment(session, source="test", company_id=company.id,
                           date=datetime(2025, 4, 1), hs_code="0903",
                           qty=100.0, value_usd=250.0) is True
    assert session.query(Shipment).count() == 2


def test_source_contract_run_for_company(session):
    company = Company(canonical_name="Contract Co")
    session.add(company)
    session.flush()
    source = _FakeSource(session)
    assert source.list_companies()[0]["name"] == "Fake Co"
    stats = source.run_for_company(company)
    # row 3 duplicates row 1 INSIDE the batch - the idempotent upsert
    # collapses it, the unusable row is skipped
    assert stats["rows"] == 3 and stats["created"] == 1 and stats["skipped"] == 1
    assert session.query(Shipment).count() == 1
    # re-running the same source is a no-op (idempotent upsert)
    stats2 = source.run_for_company(company)
    assert stats2["created"] == 0


def test_source_failure_degrades(session):
    class _Down(_FakeSource):
        def fetch_shipments(self, company):
            raise RuntimeError("source down")

    company = Company(canonical_name="Down Co")
    session.add(company)
    session.flush()
    stats = _Down(session).run_for_company(company)
    assert stats == {"source": "fake", "error": "source down"}
    assert session.query(Shipment).count() == 0
