"""Generic shipment CSV importer (spec section 15, M7).

"The user may later find sources for other countries; they plug in through
the ShipmentSource interface" - and the US connector's named fallback is a
CSV import. The column mapping lives in config/shipments.yaml
(csv_import.columns): canonical field -> alias list, so any country's
export layout maps without code changes (same pattern as the Trade Map
importer: delimiter sniffing, preamble skip, tolerant numbers/dates).

Companies are matched-or-created: a shipment row is itself buyer evidence,
so an unknown consignee becomes a minimal company (dedupe sweep may merge
it later).

Run: uv run buyerintel shipments-import --file path\to\bol.csv
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.config import load_yaml
from buyerintel.db.models import Company, CompanyAlias
from buyerintel.resolve.normalize import normalize_name
from buyerintel.shipments.base import parse_date, to_float, upsert_shipment
from buyerintel.textio import read_text_any

DEFAULT_COLUMNS = {
    "company": ["consignee", "consignee name", "company", "buyer", "company name",
                "importer", "importer name"],
    "date": ["date", "shipment date", "arrival date", "bill of lading date", "bol date"],
    "hs_code": ["hs code", "hs", "hscode", "commodity code", "tariff code"],
    "product_desc": ["product description", "description", "goods", "goods description",
                     "product", "commodity"],
    "qty": ["quantity", "qty", "net weight (kg)", "net weight kg", "weight kg", "kg"],
    "unit": ["unit", "qty unit", "unit of measure", "uom"],
    "value_usd": ["value usd", "value (usd)", "declared value", "value", "invoice value"],
    "origin": ["origin", "country of origin", "shipper country"],
    "dest": ["dest", "destination", "destination country", "consignee country"],
    "shipper": ["shipper", "supplier", "exporter", "shipper name"],
}


@dataclass
class ImportStats:
    rows_read: int = 0
    shipments_created: int = 0
    companies_created: int = 0
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"rows={self.rows_read} shipments_created={self.shipments_created} "
                f"companies_created={self.companies_created} "
                f"skipped={len(self.skipped)}")


def sniff_delimiter(sample: str) -> str:
    counts = {d: sample.count(d) for d in (",", ";", "\t")}
    return max(counts, key=lambda d: counts[d])


def skip_preamble(lines: list[str]) -> int:
    """Index of the header line (first line with >= 3 fields)."""
    for i, line in enumerate(lines):
        if len(line.split(sniff_delimiter(line))) >= 3:
            return i
    return 0


def build_column_map(mapping: dict | None) -> dict[str, str]:
    """canonical -> alias map from config, falling back to the defaults."""
    columns = dict(DEFAULT_COLUMNS)
    for canonical, aliases in (mapping or {}).items():
        columns[canonical] = [str(a).lower() for a in aliases]
    alias_to_canonical: dict[str, str] = {}
    for canonical, aliases in columns.items():
        for alias in aliases:
            alias_to_canonical[alias] = canonical
        alias_to_canonical[canonical] = canonical
    return alias_to_canonical


def match_or_create_company(session: Session, name: str) -> tuple[Company | None, bool]:
    """Find the company by normalized canonical name or alias; else create."""
    clean = (name or "").strip()
    if not clean:
        return None, False
    norm = normalize_name(clean)
    for company in session.scalars(select(Company)).all():
        if normalize_name(company.canonical_name) == norm:
            return company, False
        if session.scalar(
            select(CompanyAlias.id).where(CompanyAlias.company_id == company.id,
                                          CompanyAlias.alias == clean)
        ) is not None:
            return company, False
    company = Company(canonical_name=clean, tags=["shipments_csv"])
    session.add(company)
    session.flush()
    return company, True


def import_shipments_csv(session: Session, path: str | Path, *,
                         config: dict | None = None,
                         source: str = "csv") -> ImportStats:
    """Import one shipments CSV into the shipment table (idempotent)."""
    cfg = config or load_yaml("shipments.yaml")
    alias_map = build_column_map((cfg.get("csv_import") or {}).get("columns"))

    text = read_text_any(Path(path))
    lines = text.splitlines()
    start = skip_preamble(lines)
    reader = csv.DictReader(lines[start:], delimiter=sniff_delimiter(lines[start] if start < len(lines) else ""))
    if not reader.fieldnames:
        raise ValueError(f"no CSV header found in {path}")
    field_map = {}
    for name in reader.fieldnames:
        canonical = alias_map.get(name.strip().lower())
        if canonical:
            field_map[name] = canonical

    stats = ImportStats()
    for row in reader:
        stats.rows_read += 1
        values = {canon: (row.get(raw) or "").strip() for raw, canon in field_map.items()}
        company_name = values.get("company")
        if not company_name:
            stats.skipped.append(f"row{stats.rows_read}:no company")
            continue
        company, created = match_or_create_company(session, company_name)
        if created:
            stats.companies_created += 1
        shipment = {
            "date": parse_date(values.get("date")),
            "hs_code": values.get("hs_code") or None,
            "product_desc": values.get("product_desc") or None,
            "qty": to_float(values.get("qty")),
            "unit": values.get("unit") or None,
            "value_usd": to_float(values.get("value_usd")),
            "origin": values.get("origin") or None,
            "dest": values.get("dest") or None,
            "shipper": values.get("shipper") or None,
            "consignee": company_name,
        }
        if shipment["date"] is None and shipment["qty"] is None and shipment["value_usd"] is None:
            stats.skipped.append(f"row{stats.rows_read}:no date/qty/value")
            continue
        if upsert_shipment(session, source=source, company_id=company.id, **shipment):
            stats.shipments_created += 1
    session.flush()
    return stats
