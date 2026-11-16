"""Generic CSV importer: column mapping, matching, idempotency (M7)."""
from __future__ import annotations

from pathlib import Path

from buyerintel.db.models import Company, Shipment
from buyerintel.shipments.csv_import import (
    build_column_map,
    import_shipments_csv,
    match_or_create_company,
    skip_preamble,
    sniff_delimiter,
)

FIXTURE = Path(__file__).parent / "fixtures" / "shipments_sample.csv"


def test_sniff_delimiter_and_preamble():
    assert sniff_delimiter("a,b,c") == ","
    assert sniff_delimiter("a;b;c") == ";"
    assert skip_preamble(["junk line", "another", "h1,h2,h3", "a,b,c"]) == 2
    assert skip_preamble(["h1,h2,h3"]) == 0


def test_build_column_map_from_config():
    # extendable without code: a new layout's alias list just extends the yaml
    amap = build_column_map({"buyer_name": ["customer", "client"]})
    assert amap["customer"] == "buyer_name" and amap["client"] == "buyer_name"
    # defaults still available
    assert amap["consignee"] == "company"
    assert amap["hs code"] == "hs_code"


def test_match_or_create_company(session):
    session.add(Company(canonical_name="Giza Herbs Trading Co"))
    session.flush()
    found, created = match_or_create_company(session, "giza herbs trading co")
    assert found is not None and created is False

    new, created = match_or_create_company(session, "Brand New Import LLC")
    assert created is True and new.canonical_name == "Brand New Import LLC"
    assert "shipments_csv" in (new.tags or [])   # provenance tag
    # second call matches the just-created company (no duplicate)
    again, created2 = match_or_create_company(session, "brand new import llc")
    assert again.id == new.id and created2 is False


def test_import_shipments_csv(session):
    stats = import_shipments_csv(session, FIXTURE)
    assert stats.rows_read == 4                     # preamble lines skipped
    assert stats.shipments_created == 4
    assert stats.companies_created == 2             # fresh db: both consignees created
    companies = session.query(Company).all()
    assert {c.canonical_name for c in companies} == {
        "Giza Herbs Trading Co", "Cairo Spice Import GmbH"}
    giza = next(c for c in companies if c.canonical_name == "Giza Herbs Trading Co")
    rows = session.query(Shipment).filter_by(company_id=giza.id).all()
    assert len(rows) == 3
    assert rows[0].date is not None
    assert rows[0].qty == 2500.0 and rows[0].value_usd == 7125.0
    assert rows[0].consignee == "Giza Herbs Trading Co"
    assert rows[0].shipper == "Nile Exports SAE"
    assert rows[0].dest == "USA"


def test_import_is_idempotent(session):
    first = import_shipments_csv(session, FIXTURE)
    assert first.shipments_created == 4
    second = import_shipments_csv(session, FIXTURE)
    assert second.shipments_created == 0
    assert second.companies_created == 0
    assert session.query(Shipment).count() == 4


def test_import_skips_unusable_rows(session, tmp_path):
    csv = tmp_path / "broken.csv"
    csv.write_text(
        "buyer,shipment date,quantity\n"
        "Co A,2025-01-05,100\n"
        ",2025-01-06,200\n"          # no company -> skipped
        "Co B,garbage,garbage\n",    # no date/qty/value -> skipped
        encoding="utf-8")
    stats = import_shipments_csv(session, csv)
    assert stats.rows_read == 3
    assert stats.shipments_created == 1
    assert len(stats.skipped) == 2


def test_custom_config_mapping(session, tmp_path):
    """A new layout maps through config alone (spec: column-mapping YAML)."""
    csv = tmp_path / "other.csv"
    csv.write_text("customer;weight;invoice value\n"
                   "Client Co;500;1250\n", encoding="utf-8")
    cfg = {"csv_import": {"columns": {
        "company": ["customer"], "qty": ["weight"], "value_usd": ["invoice value"]}}}
    stats = import_shipments_csv(session, csv, config=cfg)
    assert stats.shipments_created == 1
    row = session.query(Shipment).one()
    assert row.qty == 500.0 and row.value_usd == 1250.0
