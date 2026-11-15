"""Generic workbook / CSV importer (spec M0, section 20).

Imports the user's existing lead lists through a per-source column-mapping YAML.
Idempotency: every row is hashed canonically into ``source_record.raw_hash``;
re-importing the same file creates zero duplicates and still audits every change.

Mapping file shape (config or ad hoc):

    source: anuga_2024
    sheet: "Exhibitors"          # optional for Excel, ignored for CSV
    columns:
      name: "Company Name"       # required -> company.canonical_name
      country: "Country"         # optional -> company.country (ISO-2 or name)
      domain: "Website"          # optional -> company.domain
      city: "City"
      address: "Address"
      legal_name: "Legal Name"
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml
from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.db.models import (
    Company,
    CompanyAlias,
    ContactPoint,
    Expo,
    ExpoEdition,
    SourceRecord,
)
from buyerintel.resolve.normalize import normalize_domain, normalize_name

FIELD_ALIASES = {
    "name": "canonical_name",
    "legal_name": "legal_name",
    "domain": "domain",
    "country": "country",
    "city": "city",
    "address": "address",
}

@dataclass
class ImportStats:
    rows_total: int = 0
    rows_new: int = 0
    rows_duplicate: int = 0
    companies_created: int = 0
    companies_linked_existing: int = 0
    aliases_created: int = 0

    def summary(self) -> str:
        return (
            f"rows={self.rows_total} new={self.rows_new} duplicate={self.rows_duplicate} "
            f"companies_created={self.companies_created} "
            f"companies_linked_existing={self.companies_linked_existing} "
            f"aliases={self.aliases_created}"
        )


def canonical_row_hash(row: dict) -> str:
    """Stable hash of a normalized row; identical rows share a hash."""
    canon = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def read_rows(path: Path, sheet: str | None = None) -> list[dict]:
    """Read an xlsx/xls/csv file into a list of {header: value} dicts."""
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        headers = [str(h).strip() if h is not None else f"col{i}" for i, h in enumerate(rows[0])]
        return [
            {headers[i]: cell for i, cell in enumerate(r) if i < len(headers)}
            for r in rows[1:]
            if any(c is not None and str(c).strip() for c in r)
        ]
    if suffix == ".csv":
        import csv

        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            return [
                {k: v for k, v in row.items() if k is not None and str(v or "").strip()}
                for row in reader
                if any(str(v or "").strip() for v in row.values())
            ]
    raise ValueError(f"Unsupported import format: {suffix}")


def _cell(value) -> str:
    return str(value).strip() if value is not None else ""


def _pick(row: dict, header) -> str:
    """Read the first matching header for a column spec (string or alias list)."""
    if isinstance(header, (list, tuple)):
        for candidate in header:
            if candidate in row:
                return _cell(row[candidate])
        return ""
    return _cell(row.get(header))


def import_workbook(
    session: Session,
    file_path: str | Path,
    mapping_path: str | Path,
    *,
    expo_name: str | None = None,
    expo_year: int | None = None,
) -> ImportStats:
    """Import a lead workbook idempotently. Re-running is a no-op (M0 acceptance)."""
    mapping = yaml.safe_load(Path(mapping_path).read_text(encoding="utf-8"))
    source = mapping["source"]
    columns = mapping.get("columns", {})
    sheet = mapping.get("sheet")

    name_col = columns.get("name")
    if not name_col:
        raise ValueError("mapping must define columns.name")

    # Optional expo registration for this source.
    edition_id: int | None = None
    if expo_name:
        expo = session.scalar(select(Expo).where(Expo.name == expo_name))
        if expo is None:
            expo = Expo(name=expo_name)
            session.add(expo)
            session.flush()
        if expo_year is not None:
            edition = session.scalar(
                select(ExpoEdition).where(
                    ExpoEdition.expo_id == expo.id, ExpoEdition.year == expo_year
                )
            )
            if edition is None:
                edition = ExpoEdition(expo_id=expo.id, year=expo_year)
                session.add(edition)
                session.flush()
            edition_id = edition.id

    rows = read_rows(Path(file_path), sheet=sheet)
    stats = ImportStats(rows_total=len(rows))

    # Index existing raw hashes for this source and existing companies by key.
    known_hashes: set[str] = set(
        session.scalars(select(SourceRecord.raw_hash).where(SourceRecord.source == source))
    )
    companies_by_name: dict[str, Company] = {
        c.canonical_name: c for c in session.scalars(select(Company))
    }

    for row in rows:
        mapped: dict = {}
        for field_name, header in columns.items():
            value = _pick(row, header)
            if value:
                mapped[field_name] = value
        raw_name = mapped.get("name", "")
        if not raw_name:
            stats.rows_duplicate += 1 if not row else 0
            continue

        raw_hash = canonical_row_hash(
            {k: v for k, v in row.items() if v not in (None, "")}
        )
        if raw_hash in known_hashes:
            stats.rows_duplicate += 1
            continue

        canonical = normalize_name(raw_name)
        if not canonical:
            stats.rows_duplicate += 1
            continue

        domain = normalize_domain(mapped.get("domain"))

        company = companies_by_name.get(canonical)
        created = company is None
        if created:
            company = Company(
                canonical_name=canonical,
                legal_name=mapped.get("legal_name") or None,
                domain=domain,
                country=mapped.get("country") or None,
                city=mapped.get("city") or None,
                address=mapped.get("address") or None,
                confidence=0.5,
            )
            session.add(company)
            session.flush()
            companies_by_name[canonical] = company
            stats.companies_created += 1
        else:
            stats.companies_linked_existing += 1
            if canonical != raw_name and raw_name:
                alias = CompanyAlias(
                    company_id=company.id, alias=raw_name, kind="trade"
                )
                session.add(alias)
                stats.aliases_created += 1

        session.add(
            SourceRecord(
                source=source,
                raw_hash=raw_hash,
                raw_json={k: v for k, v in row.items() if v not in (None, "")},
                company_id=company.id,
                expo_edition_id=edition_id,
            )
        )

        # Optional published phone column -> company contact point (M1).
        phone = mapped.get("phone")
        if phone and company.id:
            existing = session.scalar(
                select(ContactPoint).where(
                    ContactPoint.owner_type == "company",
                    ContactPoint.owner_id == company.id,
                    ContactPoint.kind == "phone",
                    ContactPoint.value == phone,
                )
            )
            if existing is None:
                session.add(
                    ContactPoint(
                        owner_type="company",
                        owner_id=company.id,
                        kind="phone",
                        value=phone,
                        verified_status="published",
                        confidence=0.6,
                    )
                )

        known_hashes.add(raw_hash)
        stats.rows_new += 1

    session.flush()
    return stats
