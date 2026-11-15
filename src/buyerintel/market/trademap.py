"""ITC Trade Map importer (user-downloaded CSV files).

ITC Trade Map has no free API, so the user downloads a CSV per
product/country/year from trademap.org and drops it here; the importer
sniffs the delimiter, skips the preamble, normalizes column aliases, and
upserts trade_flow rows. Column names differ by export layout (annual,
monthly, "bilateral" views) so aliases are broad.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from buyerintel.market.common import upsert_trade_flow

COLUMN_ALIASES = {
    "reporter": ["reporter", "reportername", "reporters", "reporter_name"],
    "partner": ["partner", "partnername", "partners", "partner_name"],
    "hs": ["code", "hs", "product code", "productcode", "hs code", "cmdcode", "hs6"],
    "period": ["year", "period", "year period", "month", "date"],
    "value": [
        "trade value (usd) thousand",
        "trade value (usd)",
        "trade value thousand usd",
        "export value in thousands usd",
        "import value in thousands usd",
        "value in current thousand usd",
        "trade value thousand",
    ],
    "qty": [
        "trade quantity (kg)",
        "quantity in kg",
        "quantity (kg)",
        "netweight (kg)",
        "net weight (kg)",
        "netweight",
    ],
    "unit": ["unit", "unit type", "qty unit", "unitcode"],
}


@dataclass
class TrademapStats:
    rows_read: int = 0
    flows_created: int = 0
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return f"trademap rows={self.rows_read} flows_created={self.flows_created}"


def sniff_delimiter(sample: str) -> str:
    """Comma, semicolon, or tab - whichever splits the header into most fields."""
    counts = {d: sample.count(d) for d in (",", ";", "\t")}
    return max(counts, key=lambda d: counts[d])


def skip_preamble(lines: list[str]) -> int:
    """Return the index of the CSV header line (first line with >2 separators)."""
    for i, line in enumerate(lines):
        if len(line.split(sniff_delimiter(line))) >= 3:
            return i
    return 0


def _canonical(col: str) -> str | None:
    key = col.strip().lower()
    for canonical, aliases in COLUMN_ALIASES.items():
        if key in aliases:
            return canonical
    return None


def import_trademap_csv(
    session: Session,
    path: str | Path,
    *,
    default_reporter: str = "EGY",
    source: str = "trademap",
) -> TrademapStats:
    """Import one Trade Map export into trade_flow (idempotent upserts)."""
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    lines = text.splitlines()
    start = skip_preamble(lines)
    header = lines[start] if start < len(lines) else ""
    delimiter = sniff_delimiter(header)
    reader = csv.DictReader(lines[start:], delimiter=delimiter)
    if not reader.fieldnames:
        raise ValueError(f"no CSV header found in {path}")
    columns = {name: _canonical(name) for name in reader.fieldnames if _canonical(name)}

    stats = TrademapStats()
    for row in reader:
        stats.rows_read += 1
        values = {}
        for name, canonical in columns.items():
            raw = (row.get(name) or "").strip()
            if not raw:
                continue
            raw = raw.replace(",", "").replace(" ", "")  # ITC uses "1,234" or "1 234"
            if canonical in ("value", "qty"):
                try:
                    values[canonical] = float(raw)
                except ValueError:
                    stats.skipped.append(f"row{stats.rows_read}:{canonical}='{raw}'")
                    values[canonical] = None
            else:
                values[canonical] = raw
        hs = values.get("hs")
        period = values.get("period")
        if not hs or not period:
            stats.skipped.append(f"row{stats.rows_read}:missing hs/period")
            continue
        reporter = values.get("reporter") or default_reporter
        partner = values.get("partner") or "WLD"
        created = upsert_trade_flow(
            session,
            reporter=str(reporter)[:8],
            partner=str(partner)[:8],
            hs=str(hs)[:16],
            period=str(period)[:16],
            value_usd=values.get("value"),
            qty=values.get("qty"),
            source=source,
        )
        if created:
            stats.flows_created += 1
    session.flush()
    return stats
