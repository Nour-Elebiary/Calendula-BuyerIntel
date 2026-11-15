"""Eurostat Comext connector (dedicated Comext API, JSON-stat 2.0).

Verified endpoint shape (checked 2026-11-15 against Eurostat's API guide and the
comext SDK docs): DS- datasets are served on the dedicated base

    https://ec.europa.eu/eurostat/api/comext/dissemination/statistics/1.0/data/{dataset}
        ?format=JSON&lang=EN&freq=A&reporter=EU27_2020&partner=EG
        &flow=1&product=090961&time=2023&time=2024

DS-045409 = EU trade since 1988 by HS2-4-6 and CN8. Product codes are 2/4/6/8
digit (our 6-digit HS codes pass as-is). Indicators: VALUE_IN_EUROS and
QUANTITY_IN_100KG (x100 = kg). Partner/flow dims use Eurostat codes ("EG",
"1" imports, "2" exports). EUR values are converted to USD with the latest
Frankfurter EUR:USD rate when available (flow values stay None otherwise).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product as cartesian
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.config import load_yaml
from buyerintel.db.models import HsCandidate, MarketSignal
from buyerintel.market.common import polite_get, upsert_trade_flow


@dataclass
class ComextStats:
    calls: int = 0
    observations: int = 0
    flows_created: int = 0
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"comext calls={self.calls} observations={self.observations} "
            f"flows_created={self.flows_created}"
        )


def decode_jsonstat(payload: dict) -> list[dict]:
    """Decode a JSON-stat 2.0 cube into flat rows {dim: code, "_value": value}."""
    ids: list[str] = payload["id"]
    sizes: list[int] = payload["size"]
    codes: dict[str, list[str]] = {}
    for dim in ids:
        index = payload["dimension"][dim]["category"]["index"]
        codes[dim] = sorted(index.keys(), key=lambda c: index[c])
    rows: list[dict] = []
    values = payload.get("value") or {}
    # Linear index -> dimension positions (row-major over ids).
    strides = [1] * len(sizes)
    for i in range(len(sizes) - 2, -1, -1):
        strides[i] = strides[i + 1] * sizes[i + 1]
    for raw_key, raw_value in values.items():
        linear = int(raw_key)
        positions = [linear // s % n for s, n in zip(strides, sizes)]
        row = {dim: codes[dim][pos] for dim, pos in zip(ids, positions)}
        row["_value"] = raw_value
        rows.append(row)
    return rows


def _eur_to_usd_rate(session: Session) -> float | None:
    """Latest EUR:USD rate from the fx signals (1/USD:EUR), if stored."""
    signal = session.scalar(
        select(MarketSignal)
        .where(MarketSignal.source == "fx", MarketSignal.subject == "EUR:USD")
        .order_by(MarketSignal.period.desc(), MarketSignal.id.desc())
        .limit(1)
    )
    return signal.value if signal is not None else None


def _catalog_hs_list(session: Session) -> list[str]:
    return sorted({c for c in session.scalars(select(HsCandidate.hs_code)).all() if c})


def fetch_comext(
    session: Session,
    *,
    years: Iterable[int] | None = None,
    hs_codes: Iterable[str] | None = None,
    flow: str | None = None,
    max_calls: int | None = None,
) -> ComextStats:
    """Fetch EU27 imports (default) from Egypt per catalog HS code."""
    cfg = load_yaml("market.yaml").get("comext") or {}
    base_url = cfg.get(
        "base_url", "https://ec.europa.eu/eurostat/api/comext/dissemination/statistics/1.0/data"
    )
    dataset = cfg.get("dataset", "DS-045409")
    reporter = cfg.get("reporter", "EU27_2020")
    partner = cfg.get("partner", "EG")
    flow = str(flow if flow is not None else cfg.get("import_flow", "1"))
    indicators = cfg.get("indicators") or {"value": "VALUE_IN_EUROS", "qty": "QUANTITY_IN_100KG"}
    years = list(years or [2023, 2024, 2025])
    hs_list = list(hs_codes) if hs_codes is not None else _catalog_hs_list(session)
    max_calls = int(max_calls or len(hs_list))
    rate = _eur_to_usd_rate(session)
    stats = ComextStats()

    for hs in hs_list[:max_calls]:
        params = {
            "format": "JSON",
            "lang": "EN",
            "freq": "A",
            "reporter": reporter,
            "partner": partner,
            "flow": flow,
            "product": hs,  # one product per call keeps the cube small
        }
        params.update({"time": [str(y) for y in years]})
        # httpx expands dict values that are lists into repeated query params
        # (e.g. &time=2023&time=2024), which the Comext API requires.
        response = polite_get(f"{base_url}/{dataset}", params=params)
        stats.calls += 1
        payload = response.json()
        obs = decode_jsonstat(payload)
        by_key: dict[tuple[str, str], dict] = {}
        for row in obs:
            key = (str(row.get("product") or hs), str(row.get("time")))
            by_key.setdefault(key, {})[str(row.get("indicator"))] = row["_value"]
        for (product, period), values in by_key.items():
            stats.observations += 1
            value_eur = values.get(indicators["value"])
            qty_100kg = values.get(indicators["qty"])
            value_usd = (
                round(float(value_eur) * rate, 2)
                if value_eur is not None and rate is not None
                else None
            )
            qty_kg = float(qty_100kg) * 100 if qty_100kg is not None else None
            created = upsert_trade_flow(
                session,
                reporter="EU27",
                partner="EGY",
                hs=product,
                period=period,
                value_usd=value_usd,
                qty=qty_kg,
                source="comext",
            )
            if created:
                stats.flows_created += 1
    session.flush()
    return stats
