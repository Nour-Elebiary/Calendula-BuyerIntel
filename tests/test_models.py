from __future__ import annotations

import pytest
from sqlalchemy import inspect, select

from buyerintel.db.models import (
    Company,
    ContactPoint,
    Person,
    Product,
    ProductName,
    ScoreRun,
    Shipment,
    TradeFlow,
)


def test_all_spec_tables_exist(engine):
    tables = set(inspect(engine).get_table_names())
    required = {
        "company",
        "company_alias",
        "source_record",
        "expo",
        "expo_edition",
        "expo_appearance",
        "person",
        "contact_point",
        "product",
        "product_name",
        "hs_candidate",
        "product_match",
        "persona",
        "company_persona",
        "score_run",
        "shipment",
        "trade_flow",
        "archive_snapshot",
        "outreach_event",
        "quote",
        "sample",
        "content_item",
        "content_suggestion",
        "job",
        "job_run",
        "search_cache",
        "llm_call_log",
        "audit_log",
        "schema_version",
    }
    missing = required - tables
    assert not missing, f"missing tables: {missing}"


def test_wal_mode_enabled(engine):
    with engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        assert mode == "wal"


def test_company_roundtrip(session):
    company = Company(canonical_name="test importer gmbh", country="DE", tier=2)
    session.add(company)
    session.commit()
    loaded = session.scalar(select(Company).where(Company.id == company.id))
    assert loaded is not None
    assert loaded.canonical_name == "test importer gmbh"
    assert loaded.state == "active"


def test_contact_point_uniqueness(session):
    session.add(Company(canonical_name="acme"))
    session.flush()
    cp = ContactPoint(
        owner_type="company",
        owner_id=1,
        kind="email",
        value="info@acme.example",
        verified_status="published",
    )
    session.add(cp)
    session.commit()
    session.add(
        ContactPoint(owner_type="company", owner_id=1, kind="email", value="info@acme.example")
    )
    with pytest.raises(Exception):
        session.commit()
    session.rollback()


def test_shipment_and_tradeflow_nullable_fields(session):
    session.add(Shipment(source="test", shipper="s", consignee="c"))
    session.add(TradeFlow(reporter="EG", partner="US", hs="0904", period="2025-01"))
    session.commit()
    flows = session.scalars(select(TradeFlow)).all()
    assert len(flows) == 1
    assert flows[0].value_usd is None


def test_score_run_stores_components_json(session):
    session.add(Company(canonical_name="acme"))
    session.flush()
    run = ScoreRun(
        company_id=1,
        components={"product_demand_fit": 25, "chain_role": 10},
        total=70.0,
        tier=1,
        weights_version=1,
    )
    session.add(run)
    session.commit()
    loaded = session.scalar(select(ScoreRun))
    assert loaded.components["product_demand_fit"] == 25


def test_product_multilingual_names(session):
    p = Product(name="Hibiscus", latin_name="Hibiscus sabdariffa", organic=True)
    session.add(p)
    session.flush()
    session.add_all(
        [
            ProductName(product_id=p.id, language="en", kind="common", name="hibiscus"),
            ProductName(product_id=p.id, language="ar", kind="synonym", name="كركديه"),
            ProductName(product_id=p.id, language="de", kind="synonym", name="Roselle"),
        ]
    )
    session.commit()
    names = session.scalars(
        select(ProductName).where(ProductName.product_id == p.id)
    ).all()
    assert len(names) == 3
    assert any(n.language == "ar" for n in names)
