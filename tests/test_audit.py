from __future__ import annotations

from sqlalchemy import select

from buyerintel.db.audit import TRACKED
from buyerintel.db.models import AuditLog, Company


def test_audit_covers_all_model_tables_except_audit_log():
    assert "audit_log" not in TRACKED
    assert "company" in TRACKED
    assert "person" in TRACKED
    assert "shipment" in TRACKED


def test_insert_is_audited(session):
    session.add(Company(canonical_name="audit me", country="EG"))
    session.commit()
    rows = session.scalars(
        select(AuditLog).where(AuditLog.table_name == "company", AuditLog.row_id == "1")
    ).all()
    fields = {r.field: r for r in rows}
    assert "canonical_name" in fields
    assert fields["canonical_name"].old_value is None
    assert "audit me" in fields["canonical_name"].new_value
    assert fields["canonical_name"].source == "test"


def test_update_records_old_and_new(session):
    company = Company(canonical_name="acme", tier=None)
    session.add(company)
    session.commit()
    session.info["audit_source"] = "scorer"
    company.tier = 1
    company.state = "parked"
    session.commit()

    changes = session.scalars(
        select(AuditLog).where(
            AuditLog.table_name == "company",
            AuditLog.row_id == str(company.id),
            AuditLog.field.in_(["tier", "state"]),
        )
    ).all()
    by_field = {c.field: c for c in changes}
    assert by_field["tier"].old_value in (None, "null")
    assert by_field["tier"].new_value == "1"
    assert by_field["tier"].source == "scorer"
    assert by_field["state"].old_value == '"active"'
    assert by_field["state"].new_value == '"parked"'


def test_delete_is_audited(session):
    company = Company(canonical_name="doomed")
    session.add(company)
    session.commit()
    row_id = company.id
    session.delete(company)
    session.commit()
    rows = session.scalars(
        select(AuditLog).where(
            AuditLog.table_name == "company",
            AuditLog.row_id == str(row_id),
            AuditLog.new_value.is_(None),
        )
    ).all()
    assert rows, "deletion must record null new_value"
    assert any(r.field == "canonical_name" for r in rows)


def test_audit_source_defaults(session):
    session.info.pop("audit_source", None)
    session.add(Company(canonical_name="no source given"))
    session.commit()
    row = session.scalar(select(AuditLog).where(AuditLog.field == "canonical_name"))
    assert row.source == "unknown"
