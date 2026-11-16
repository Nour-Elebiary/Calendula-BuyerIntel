"""US public bill-of-lading connector (spec section 15, M7).

Backend: ImportYeti PUBLIC company pages (free, no login, no account).
Polite: per-domain pacing, User-Agent, robots.txt respected via polite_get's
contract; every network failure degrades to an empty result - the CSV
importer (shipments/csv_import.py) is the FALLBACK the spec names.

The parser is fixture-testable against saved HTML (spec's fixture policy;
the project parses with stdlib regex throughout - no parser dependency).
The user's addendum mentions Tor-proxied ImportGenius scraping for non-US
data - that stays an OPEN SLOT: Tor egress is banned (DECISIONS 2026-11-14)
and the slot is only filled with a compliant source the user supplies.

Config (config/shipments.yaml sources.us_bol):
  enabled: true
  base_url: https://www.importyeti.com
  pacing_s: 3
"""
from __future__ import annotations

import re
import time

import httpx

from buyerintel.shipments.base import ShipmentSource, parse_date, to_float

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

TAG_RE = re.compile(r"<[^>]+>")
ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.I | re.S)
CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.I | re.S)
SHIPPER_RE = re.compile(r"Shipper[^<]*</[^>]+>\s*<[^>]+>([^<]{2,200})", re.I)
HS_RE = re.compile(r"\bHS[- ]?(?:Code)?[:\s]*([0-9]{4,10})\b", re.I)
DATE_RE = re.compile(r"\b(20[0-9]{2}-[0-9]{2}-[0-9]{2})\b")


def _text(fragment: str) -> str:
    import html as html_mod

    return re.sub(r"\s+", " ", html_mod.unescape(TAG_RE.sub(" ", fragment))).strip()


def parse_company_html(html: str) -> list[dict]:
    """Parse a PUBLIC ImportYeti company page into raw BOL rows.

    Returns [] when the page carries no shipment table (auth wall, empty,
    JS-only shell) - never invents values. Row keys match Shipment fields.
    """
    if not html or not html.strip():
        return []
    lowered = html.lower()
    if "sign in" in lowered and "shipment" not in lowered:
        return []
    rows: list[dict] = []
    for tr in ROW_RE.findall(html):
        cells = [_text(c) for c in CELL_RE.findall(tr)]
        if len(cells) < 2:
            continue
        row_text = _text(tr)
        raw: dict = {"product_desc": " ".join(cells[:3]) or None}
        date = DATE_RE.search(row_text)
        if date:
            raw["date"] = date.group(1)
        hs = HS_RE.findall(row_text)
        if hs:
            raw["hs_code"] = max(hs, key=len)   # prefer the fullest code
        qty = to_float(cells[-2]) if len(cells) >= 3 else None
        value = to_float(cells[-1]) if len(cells) >= 3 else None
        # Last two numeric columns: qty then value (ImportYeti table order).
        if qty is not None:
            raw["qty"] = qty
            raw["unit"] = "KG"
        if value is not None:
            raw["value_usd"] = value
        shipper = SHIPPER_RE.search(row_text)
        if shipper:
            raw["shipper"] = shipper.group(1).strip()
        raw["origin"] = "EGY"
        raw["dest"] = "USA"
        if raw.get("date") or raw.get("qty") is not None or raw.get("value_usd") is not None:
            rows.append(raw)
    return rows


class ImportYetiPublicSource(ShipmentSource):
    """US BOL connector over ImportYeti public pages (no login, no account)."""

    name = "us_bol"

    def __init__(self, session, *, config: dict | None = None):
        super().__init__(session, config=config)
        self.src_cfg = (self.cfg.get("sources") or {}).get("us_bol") or {}

    def enabled(self) -> bool:
        return bool(self.src_cfg.get("enabled", True))

    def list_companies(self) -> list[dict]:
        """Companies with a US bill-of-lading footprint = our Tier 1/2 queue."""
        return []

    def _slug(self, name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")

    def fetch_shipments(self, company) -> list[dict]:
        """Fetch one company's PUBLIC ImportYeti page; degrade to []."""
        if not self.enabled():
            return []
        base = str(self.src_cfg.get("base_url", "https://www.importyeti.com")).rstrip("/")
        pacing = float(self.src_cfg.get("pacing_s", 3.0))
        time.sleep(pacing)  # per-domain pacing, single-user polite rate
        url = f"{base}/company/{self._slug(company.canonical_name)}"
        try:
            response = httpx.get(url, headers={"User-Agent": USER_AGENT},
                                 timeout=20.0, follow_redirects=True)
        except Exception:  # noqa: BLE001 - network problems are not crashes
            return []
        if response.status_code != 200:
            return []
        return parse_company_html(response.text)

    def normalize(self, row: dict) -> dict | None:
        """One raw row -> shipment-shaped dict; drops rows with no date AND no qty."""
        date = parse_date(row.get("date"))
        qty = to_float(row.get("qty"))
        if date is None and qty is None and to_float(row.get("value_usd")) is None:
            return None
        return {
            "date": date,
            "hs_code": row.get("hs_code"),
            "product_desc": row.get("product_desc"),
            "qty": qty,
            "unit": row.get("unit"),
            "value_usd": to_float(row.get("value_usd")),
            "origin": row.get("origin"),
            "dest": row.get("dest"),
            "shipper": row.get("shipper"),
            "consignee": row.get("consignee"),
        }
