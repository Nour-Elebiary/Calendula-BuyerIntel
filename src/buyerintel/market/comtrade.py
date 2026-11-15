"""UN Comtrade connector (free preview API).

Verified endpoint shape (checked 2026-11-15 against comtradeplus.un.org/en/api-docs
and live probes documented by the community):

    GET https://comtradeapi.un.org/public/v1/preview/C/A/HS
        ?reporterCode=818&period=2024&flowCode=X,M
        &cmdCode=0909&partnerCode=0,842,...

Free-tier constraints baked into this implementation:
- ONE period per call (multi-period is rejected outright).
- Max 1 commodity code per call on /preview (multi value is tariffline-only).
- 500 rows per call, no pagination; partner list is capped well below that.
- ~1 req/s throttle with a silent 429 -> inter-call pacing + retry with backoff.
- Descriptive fields come back null; only numeric codes -> local partner map.
- Duplicate rows per partner (different customsCode/mosCode) -> deduplicated,
  preferring the general-trade row (customsCode C00), then the largest value.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.config import load_yaml
from buyerintel.db.models import HsCandidate
from buyerintel.market.common import hs_to_iso, polite_get, upsert_trade_flow

EGYPT_REPORTER = "818"


@dataclass
class ComtradeStats:
    calls: int = 0
    rows_fetched: int = 0
    flows_created: int = 0
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"comtrade calls={self.calls} rows={self.rows_fetched} "
            f"flows_created={self.flows_created}"
        )


def _catalog_hs_list(session: Session) -> list[str]:
    """Every HS code configured for the catalog (low-confidence ones included)."""
    return sorted({c for c in session.scalars(select(HsCandidate.hs_code)).all() if c})


def _dedupe_rows(rows: list[dict]) -> dict[tuple[str, str], dict]:
    """One row per (partner, flow): prefer general trade (C00), then largest value."""

    def rank(row: dict) -> tuple[int, float]:
        customs_preference = 1 if (row.get("customsCode") in (None, "C00")) else 0
        return (customs_preference, float(row.get("primaryValue") or 0.0))

    best: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (str(row.get("partnerCode")), str(row.get("flowCode")).upper())
        if key not in best or rank(row) > rank(best[key]):
            best[key] = row
    return best


def fetch_comtrade(
    session: Session,
    *,
    years: Iterable[int] | None = None,
    flow_codes: Iterable[str] | None = None,
    hs_codes: Iterable[str] | None = None,
    max_calls: int | None = None,
) -> ComtradeStats:
    """Fetch Egypt exports/imports for the catalog HS list into trade_flow."""
    cfg = load_yaml("market.yaml").get("comtrade") or {}
    base_url = cfg.get("base_url", "https://comtradeapi.un.org/public/v1/preview")
    reporter = str(cfg.get("egypt_reporter_code", EGYPT_REPORTER))
    partner_map = cfg.get("partners") or {}
    years = list(years or cfg.get("years") or [])
    flows = ",".join(f.upper() for f in (flow_codes or cfg.get("flow_codes") or ["X", "M"]))
    hs_list = list(hs_codes) if hs_codes is not None else _catalog_hs_list(session)
    max_calls = int(max_calls if max_calls is not None else cfg.get("max_calls_per_run", 40))
    pause = float(cfg.get("inter_call_seconds", 1.2))
    stats = ComtradeStats()

    for hs in hs_list:
        for year in years:
            if stats.calls >= max_calls:
                stats.skipped.append(f"{hs}/{year}:call-budget")
                continue
            if stats.calls:
                time.sleep(pause)  # preview API throttles ~1 req/s
            params = {
                "reporterCode": reporter,
                "period": str(year),  # one period per call (API constraint)
                "flowCode": flows,
                "cmdCode": hs,  # one commodity per call (API constraint)
                "partnerCode": ",".join(partner_map.keys()) if partner_map else "0",
            }
            response = polite_get(f"{base_url}/C/A/HS", params=params)
            stats.calls += 1
            rows = response.json().get("data") or []
            for row in _dedupe_rows(rows).values():
                stats.rows_fetched += 1
                partner_iso = hs_to_iso(partner_map, str(row.get("partnerCode")))
                if partner_iso is None:
                    continue
                value = row.get("primaryValue")
                qty = row.get("netWgt")
                created = upsert_trade_flow(
                    session,
                    reporter="EGY",
                    partner=partner_iso,
                    hs=str(row.get("cmdCode") or hs),
                    period=str(row.get("period") or year),
                    value_usd=float(value) if value is not None else None,
                    qty=float(qty) if qty else None,
                    source="comtrade",
                )
                if created:
                    stats.flows_created += 1
    session.flush()
    return stats
