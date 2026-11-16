"""ShipmentSource interface and shared upsert (spec section 15).

Interface (exactly the spec's shape):
  list_companies()               -> candidates this source can cover
  fetch_shipments(company)       -> raw rows (dicts) for one company
  normalize(row)                 -> shipment-shaped dict (or None to drop)

Every raw row passes through `normalize()` before `upsert_shipment()`
persists it; the upsert is idempotent (same source + company + date + hs +
qty + value = same shipment, never duplicated).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.config import load_yaml
from buyerintel.db.models import Company, Shipment


class ShipmentSource(ABC):
    """One shipment data provider (spec section 15)."""

    name: str = "abstract"

    def __init__(self, session: Session, *, config: dict | None = None):
        self.session = session
        self.cfg = config or load_yaml("shipments.yaml")

    @abstractmethod
    def list_companies(self) -> list[dict]:
        """Company candidates this source can cover: [{name, country, ...}]."""

    @abstractmethod
    def fetch_shipments(self, company: Company) -> list[dict]:
        """Raw shipment rows for one company (unnormalized dicts)."""

    @abstractmethod
    def normalize(self, row: dict) -> dict | None:
        """One raw row -> shipment-shaped dict, or None when unusable.

        Shipment-shaped keys (matching db.models.Shipment):
        date (datetime|None), hs_code, product_desc, qty, unit, value_usd,
        origin, dest, shipper, consignee.
        """

    # ------------------------------------------------------------------ run
    def run_for_company(self, company: Company) -> dict:
        """Fetch + normalize + persist; returns a small stats dict."""
        created = skipped = 0
        try:
            rows = self.fetch_shipments(company)
        except Exception as exc:  # noqa: BLE001 - a source down is never fatal
            return {"source": self.name, "error": str(exc)[:200]}
        for row in rows:
            shipment = self.normalize(row)
            if shipment is None:
                skipped += 1
                continue
            if upsert_shipment(self.session, source=self.name,
                               company_id=company.id, **shipment):
                created += 1
        self.session.flush()
        return {"source": self.name, "rows": len(rows),
                "created": created, "skipped": skipped}


def parse_date(value) -> datetime | None:
    """Tolerant date parsing (ISO first, then a few common BOL layouts)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S",
                "%b %d, %Y", "%d %b %Y", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def to_float(value) -> float | None:
    """Tolerant float ('1,234.5', '1.234,5' -> 1234.5); None when unusable."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(" ", "").replace("\u00a0", "")
    if not text:
        return None
    if "." in text and "," in text:
        # whichever separator comes LAST is the decimal separator
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")   # 1.234,5
        else:
            text = text.replace(",", "")                     # 1,234.5
    elif "," in text:
        parts = text.split(",")
        # "2,500" (group of 3) = thousands; "12,5" = decimal comma
        if len(parts) == 2 and len(parts[1]) != 3:
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def upsert_shipment(session: Session, *, source: str, company_id: int | None,
                    date: datetime | None = None, hs_code: str | None = None,
                    product_desc: str | None = None, qty: float | None = None,
                    unit: str | None = None, value_usd: float | None = None,
                    origin: str | None = None, dest: str | None = None,
                    shipper: str | None = None, consignee: str | None = None,
                    raw_json: dict | None = None) -> bool:
    """Idempotent insert; returns True when a new row was created.

    A duplicate is the SAME source + company + date + hs + qty + value
    (the source's own identity of a bill of lading line).
    """
    existing = session.scalar(
        select(Shipment.id).where(
            Shipment.source == source,
            Shipment.company_id == company_id,
            Shipment.date == date,
            Shipment.hs_code == hs_code,
            Shipment.qty == qty,
            Shipment.value_usd == value_usd,
        )
    )
    if existing is not None:
        return False
    session.add(Shipment(
        source=source, company_id=company_id, date=date,
        hs_code=hs_code, product_desc=product_desc, qty=qty, unit=unit,
        value_usd=value_usd, origin=origin, dest=dest,
        shipper=shipper, consignee=consignee, raw_json=raw_json,
    ))
    session.flush()
    return True
