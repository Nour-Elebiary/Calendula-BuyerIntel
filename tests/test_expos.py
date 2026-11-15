from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from openpyxl import Workbook
from sqlalchemy import func, select

from buyerintel.connectors.expos import ingest_expo
from buyerintel.db.models import (
    Company,
    CompanyAlias,
    ContactPoint,
    Expo,
    ExpoAppearance,
    ExpoEdition,
    SourceRecord,
)


def make_expo_file(path: Path, rows: list[tuple]) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Exhibitors"
    ws.append(["Exhibitor Name", "Country / Region", "Website", "City", "Phone"])
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


def test_expo_map_resolves(session, tmp_path):
    from buyerintel.connectors.expos import resolve_map

    assert resolve_map("biofach").name == "biofach.yaml"
    with pytest.raises(FileNotFoundError):
        resolve_map("no_such_expo")


def test_ingest_creates_expo_chain(session, tmp_path):
    """Row -> source_record -> expo -> edition -> expo_appearance."""
    f = make_expo_file(
        tmp_path / "biofach2024.xlsx",
        [("Kräuter Kontor Hamburg", "Germany", "kraeuter-kontor.de", "Nuremberg", None)],
    )
    result = ingest_expo(session, f, "biofach", expo_year=2024)
    session.commit()
    assert result["rows_new"] == 1
    assert result["appearances_created"] == 1
    assert session.scalar(select(func.count()).select_from(Expo)) == 1
    assert session.scalar(select(func.count()).select_from(ExpoEdition)) == 1
    assert session.scalar(select(func.count()).select_from(ExpoAppearance)) == 1
    src = session.scalar(select(SourceRecord))
    assert src.expo_edition_id is not None


def test_reingest_expo_is_noop(session, tmp_path):
    """M1 acceptance: re-ingesting an expo file is a no-op."""
    f = make_expo_file(
        tmp_path / "biofach2024.xlsx",
        [
            ("Kräuter Kontor Hamburg", "Germany", "kraeuter-kontor.de", "Nuremberg", None),
            ("Zaytoun Foods", "Egypt", None, "Cairo", "+20 100 123 4567"),
        ],
    )
    first = ingest_expo(session, f, "biofach", expo_year=2024)
    session.commit()
    second = ingest_expo(session, f, "biofach", expo_year=2024)
    session.commit()
    assert first["rows_new"] == 2
    assert second["rows_new"] == 0
    assert second["rows_duplicate"] == 2
    assert second["appearances_created"] == 0
    assert session.scalar(select(func.count()).select_from(Company)) == 2


def test_same_company_two_editions_stays_one_company(session, tmp_path):
    """Multi-edition exhibitors dedupe into one company with two appearances."""
    f1 = make_expo_file(
        tmp_path / "biofach2024.xlsx",
        [("Herbal Origins Ltd", "India", None, "Mumbai", None)],
    )
    f2 = make_expo_file(
        tmp_path / "biofach2025.xlsx",
        [("Herbal Origins", "India", None, "Mumbai", None)],
    )
    ingest_expo(session, f1, "biofach", expo_year=2024)
    session.commit()
    ingest_expo(session, f2, "biofach", expo_year=2025)
    session.commit()
    assert session.scalar(select(func.count()).select_from(Company)) == 1
    assert session.scalar(select(func.count()).select_from(ExpoAppearance)) == 2
    aliases = [a.alias for a in session.scalars(select(CompanyAlias))]
    assert "Herbal Origins" in aliases  # name variant kept as alias


def test_phone_column_creates_contact_point(session, tmp_path):
    f = make_expo_file(
        tmp_path / "anuga.xlsx",
        [("Zaytoun Foods", "Egypt", None, "Cairo", "+20 100 123 4567")],
    )
    ingest_expo(session, f, "anuga", expo_year=2024)
    session.commit()
    cp = session.scalar(select(ContactPoint))
    assert cp is not None
    assert cp.kind == "phone"
    assert cp.verified_status == "published"
