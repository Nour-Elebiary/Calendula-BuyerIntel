"""Workbook export tests (spec section 18, M8): the acceptance criterion is
'export workbook opens with all sheets' - generated from seeded fixture data,
loaded back with openpyxl."""
from __future__ import annotations

from datetime import datetime

from openpyxl import load_workbook

from buyerintel.catalog import seed_products
from buyerintel.crm.outreach import log_outreach
from buyerintel.crm.quotes import log_quote
from buyerintel.db.models import (
    Company,
    ContactPoint,
    Forecast,
    Person,
    Shipment,
    TradeFlow,
)
from buyerintel.export.workbook import expected_sheets, export_workbook
from buyerintel.score.personas import sync_personas
from buyerintel.score.scoring import score_company


def test_workbook_has_all_sheets(session):
    seed_products(session)
    sync_personas(session)
    session.commit()

    # minimal fixture data touching every sheet source
    company = Company(canonical_name="Export Fixture Co", country="DEU", tier=1,
                      tags=["herb importer"], size_band="large")
    session.add(company)
    session.flush()
    session.add(Person(company_id=company.id, full_name="Jane Buyer",
                       is_decision_maker=True))
    session.add(ContactPoint(owner_type="company", owner_id=company.id, kind="email",
                             value="jane@export.example", verified_status="verified"))
    session.add(Shipment(source="test", company_id=company.id,
                         date=datetime(2025, 6, 1), hs_code="0903", qty=1000,
                         value_usd=3000))
    session.add(TradeFlow(reporter="EGY", partner="DEU", hs="0903", period="2025-06",
                          value_usd=5000.0, qty=1800.0, unit_value=2.78))
    session.add(Forecast(company_id=company.id, product_key="0903", n_shipments=3,
                         confidence="medium"))
    log_outreach(session, company_id=company.id, channel="email", outcome="sent")
    log_quote(session, company_id=company.id, price=2.9, incoterm="CIF")
    score_company(session, company)
    session.commit()

    path = export_workbook(session, "data/test_workbook.xlsx")
    wb = load_workbook(path)
    names = set(wb.sheetnames)
    missing = set(expected_sheets()) - names
    assert missing == set(), f"missing sheets: {missing}"

    # the company's tier sheet has its row (tier may be re-assigned by the
    # scoring gates, so read it back from the object)
    tier_sheet = wb[f"Tier{company.tier}"]
    values = [row[0].value for row in tier_sheet.iter_rows(min_row=2)]
    assert company.id in values

    # contacts sheet carries the verified email
    contacts = [[c.value for c in row] for row in wb["Contacts"].iter_rows(min_row=2)]
    assert any("jane@export.example" in [str(v) for v in row] for row in contacts)

    # changelog reflects the audited changes (tier assignment, contact add...)
    changelog = list(wb["Changelog"].iter_rows(min_row=2, values_only=True))
    assert changelog, "changelog must be generated from the audit log"


def test_workbook_empty_db_still_opens(session):
    """An empty system must still produce every sheet (all headers)."""
    path = export_workbook(session, "data/test_empty.xlsx")
    wb = load_workbook(path)
    assert set(expected_sheets()) <= set(wb.sheetnames)
    assert wb["Summary"].max_row >= 1
