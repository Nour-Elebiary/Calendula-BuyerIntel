from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from openpyxl import Workbook
from sqlalchemy import func, select

from buyerintel.connectors.importer import import_workbook
from buyerintel.db.models import AuditLog, Company, SourceRecord


@pytest.fixture()
def sample_workbook(tmp_path: Path) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Exhibitors"
    ws.append(["Company Name", "Country", "Website", "City"])
    ws.append(["Fresh Spice GmbH", "DE", "https://www.freshspice.example", "Hamburg"])
    ws.append(["Karkade Trading Co Ltd", "EG", None, "Cairo"])
    ws.append(["Травы Импорт", "RU", "travy.ru", "Moscow"])
    ws.append([None, None, None, None])  # blank row must be ignored
    path = tmp_path / "leads.xlsx"
    wb.save(path)
    return path


@pytest.fixture()
def mapping(tmp_path: Path) -> Path:
    mapping = {
        "source": "test_expo_2024",
        "sheet": "Exhibitors",
        "columns": {
            "name": "Company Name",
            "country": "Country",
            "domain": "Website",
            "city": "City",
        },
    }
    path = tmp_path / "mapping.yaml"
    path.write_text(yaml.safe_dump(mapping), encoding="utf-8")
    return path


def test_import_creates_companies(session, sample_workbook, mapping):
    stats = import_workbook(session, sample_workbook, mapping)
    session.commit()
    assert stats.rows_total == 3  # blank row skipped
    assert stats.rows_new == 3
    assert stats.companies_created == 3
    names = {c.canonical_name for c in session.scalars(select(Company))}
    assert "fresh spice" in names
    assert "karkade trading" in names
    assert "травы импорт" in names  # unicode preserved


def test_reimport_is_a_noop(session, sample_workbook, mapping):
    import_workbook(session, sample_workbook, mapping)
    session.commit()
    stats2 = import_workbook(session, sample_workbook, mapping)
    session.commit()
    assert stats2.rows_new == 0
    assert stats2.rows_duplicate == 3
    assert session.scalar(select(func.count()).select_from(Company)) == 3
    assert session.scalar(select(func.count()).select_from(SourceRecord)) == 3


def test_import_audited(session, sample_workbook, mapping):
    session.info["audit_source"] = "import:test_expo_2024"
    import_workbook(session, sample_workbook, mapping)
    session.commit()
    rows = session.scalars(
        select(AuditLog).where(AuditLog.source == "import:test_expo_2024")
    ).all()
    assert rows, "import must flow through the audit layer"
    assert any(r.table_name == "company" for r in rows)
    assert any(r.table_name == "source_record" for r in rows)


def test_partial_update_creates_new_row_but_links_company(session, sample_workbook, mapping):
    """A row that changed since the last import is a NEW raw_hash: the company is
    linked (not duplicated) and the source_record tracks both versions."""
    import_workbook(session, sample_workbook, mapping)
    session.commit()
    # Modify the file: change a city, keep the name.
    from openpyxl import load_workbook

    wb = load_workbook(sample_workbook)
    ws = wb["Exhibitors"]
    ws["D2"] = "Berlin"
    wb.save(sample_workbook)
    stats = import_workbook(session, sample_workbook, mapping)
    session.commit()
    assert stats.rows_new == 1
    assert stats.companies_created == 0
    assert stats.companies_linked_existing == 1
    assert session.scalar(select(func.count()).select_from(Company)) == 3


def test_unsupported_format_raises(session, tmp_path, mapping):
    bad = tmp_path / "nope.txt"
    bad.write_text("x")
    with pytest.raises(ValueError):
        import_workbook(session, bad, mapping)
