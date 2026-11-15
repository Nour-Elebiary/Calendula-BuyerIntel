"""Demand signal collectors (spec section 15).

- PubMed E-utilities esearch: publication counts per product (en + Latin names).
- Frankfurter (ECB): latest FX rates, stored as both "USD:CUR" and "CUR:USD".
- Google Trends via pytrends: interest-over-time per product keyword batch
  (batches of <=5 keywords; pytrends is notoriously flaky, so failures are
  logged and skipped, never fatal).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.config import load_yaml
from buyerintel.db.models import Product, ProductName
from buyerintel.market.common import polite_get, upsert_market_signal


@dataclass
class SignalStats:
    pubmed_calls: int = 0
    pubmed_signals: int = 0
    fx_signals: int = 0
    trends_signals: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"signals pubmed_calls={self.pubmed_calls} pubmed={self.pubmed_signals} "
            f"fx={self.fx_signals} trends={self.trends_signals}"
        )


def fetch_pubmed_counts(session: Session, *, year: int = 2025) -> SignalStats:
    """PubMed esearch count per product for en common name + Latin name."""
    cfg = load_yaml("market.yaml").get("pubmed") or {}
    base_url = cfg.get("base_url", "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi")
    pause = float(cfg.get("rate_limit_seconds", 0.5))
    stats = SignalStats()
    products = session.scalars(select(Product)).all()
    seen_terms: set[str] = set()
    for product in products:
        terms = [product.name]
        if product.latin_name:
            terms.append(product.latin_name)
        for term in terms:
            term = term.strip()
            if not term or term.lower() in seen_terms:
                continue
            seen_terms.add(term.lower())
            if stats.pubmed_calls:
                time.sleep(pause)  # NCBI free tier: 3 req/s
            url = f"{base_url}?db=pubmed&retmode=json&rettype=count&term={quote(term)}"
            try:
                payload = polite_get(url).json()
            except Exception as exc:  # noqa: BLE001 - one bad term must not stop the run
                stats.errors.append(f"pubmed {term}: {exc}")
                continue
            stats.pubmed_calls += 1
            count = payload.get("esearchresult", {}).get("count")
            if count is None:
                stats.errors.append(f"pubmed {term}: no count in response")
                continue
            created = upsert_market_signal(
                session,
                source="pubmed",
                subject=f"{product.name}:{term}",
                period=str(year),
                value=float(count),
                raw_json={"product": product.name, "term": term},
            )
            if created:
                stats.pubmed_signals += 1
    session.flush()
    return stats


def fetch_fx_rates(session: Session, *, period: str = "latest") -> SignalStats:
    """Frankfurter latest rates vs USD, stored both directions."""
    cfg = load_yaml("market.yaml").get("fx") or {}
    base_url = cfg.get("base_url", "https://api.frankfurter.app/latest")
    base = cfg.get("base", "USD")
    currencies = cfg.get("currencies") or ["EUR", "EGP", "CNY", "GBP"]
    stats = SignalStats()
    url = f"{base_url}?base={base}&symbols={','.join(currencies)}"
    try:
        payload = polite_get(url).json()
    except Exception as exc:  # noqa: BLE001
        stats.errors.append(f"fx: {exc}")
        return stats
    date = str(payload.get("date") or period)
    rates: dict = payload.get("rates") or {}
    raw = {"base": base, "rates": rates, "date": date}
    for currency, rate in rates.items():
        subject = f"{base}:{currency}"
        created = upsert_market_signal(
            session,
            source="fx",
            subject=subject,
            period=date,
            value=float(rate),
            raw_json=raw,
        )
        if created:
            stats.fx_signals += 1
        # Comext needs EUR->USD; store the inverse for direct lookup.
        inverse_subject = f"{currency}:{base}"
        inverse_created = upsert_market_signal(
            session,
            source="fx",
            subject=inverse_subject,
            period=date,
            value=round(1.0 / float(rate), 6) if float(rate) else None,
            raw_json=raw,
        )
        if inverse_created:
            stats.fx_signals += 1
    session.flush()
    return stats


def fetch_trends(session: Session, *, months_back: int | None = None) -> SignalStats:
    """Google Trends interest-over-time via pytrends, batches of <=5 keywords.

    pytrends frequently breaks when Google changes internals; every failure is
    recorded in stats.errors and the run continues.
    """
    cfg = load_yaml("market.yaml").get("trends") or {}
    if not cfg.get("enabled", True):
        return SignalStats()
    months_back = int(months_back or cfg.get("months_back", 12))
    timeframe = str(cfg.get("timeframe", "today 5-y"))
    stats = SignalStats()
    try:
        from pytrends.request import TrendReq

        pytrends = TrendReq(hl="en-US", tz=0)
    except Exception as exc:  # noqa: BLE001
        stats.errors.append(f"trends init: {exc}")
        return stats

    keywords = sorted(
        {
            n.name.lower()
            for n in session.scalars(
                select(ProductName).where(ProductName.kind.in_(["common", "synonym"]))
            ).all()
            if n.name
        }
    )
    for batch_start in range(0, len(keywords), 5):
        batch = keywords[batch_start : batch_start + 5]
        try:
            pytrends.build_payload(batch, timeframe=timeframe)
            frame = pytrends.interest_over_time()
        except Exception as exc:  # noqa: BLE001
            stats.errors.append(f"trends batch {batch_start}: {exc}")
            continue
        if frame is None or frame.empty:
            stats.errors.append(f"trends batch {batch_start}: empty response")
            continue
        for keyword in batch:
            if keyword not in frame.columns:
                continue
            series = frame[keyword].dropna()
            if series.empty:
                continue
            # Store the trailing 12-month mean as the current interest level.
            tail = series.tail(months_back)
            created = upsert_market_signal(
                session,
                source="trends",
                subject=keyword,
                period=str(tail.index[-1])[:10],
                value=round(float(tail.mean()), 3),
                raw_json={"keyword": keyword, "months": int(len(tail))},
            )
            if created:
                stats.trends_signals += 1
    session.flush()
    return stats


def fetch_signals(session: Session, *, include_trends: bool = True) -> SignalStats:
    """Convenience: FX first (Comext conversion depends on it), then PubMed, then trends."""
    stats = fetch_fx_rates(session)
    stats_pubmed = fetch_pubmed_counts(session)
    stats.fx_signals += stats_pubmed.fx_signals
    stats.pubmed_calls += stats_pubmed.pubmed_calls
    stats.pubmed_signals += stats_pubmed.pubmed_signals
    stats.errors.extend(stats_pubmed.errors)
    if include_trends:
        stats_trends = fetch_trends(session)
        stats.trends_signals += stats_trends.trends_signals
        stats.errors.extend(stats_trends.errors)
    return stats
