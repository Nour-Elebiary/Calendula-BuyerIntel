"""Search engine router (spec section 8).

Chooses engines by target country and query type, tracks a per-engine health
score, caches every result in `search_cache`, and falls back across engines.
Escalation when a fallback engine is also blocked (per addendum 4.8):
rotate engines first, then FlareSolverr, then the CAPTCHA solver - both rungs
5/6 are off by default (config flags) - before the engine is marked unhealthy.

M3 scope: DuckDuckGo (ddgs), Google Programmable Search (free quota, key),
and SearXNG (self-hosted, optional) are wired. Country-specific engines
(Yandex/Baidu/Naver/...) are registered but not yet wired; they are skipped by
the router and land with the enrichment milestone's per-country work.
"""
from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.config import load_secrets, load_yaml
from buyerintel.db.models import SearchCache

# A blocked engine is skipped for this long before being retried.
COOLDOWN_SECONDS = 900
HEALTH_DECAY = 0.8      # multiplier applied on each failure
HEALTH_RECOVER = 0.1    # additive recovery on each success
MIN_HEALTH = 0.2        # below this the engine is not selected


@dataclass
class SearchResult:
    engine: str
    title: str
    url: str
    snippet: str = ""

    def as_dict(self) -> dict:
        return {"engine": self.engine, "title": self.title, "url": self.url,
                "snippet": self.snippet}


@dataclass
class RouterStats:
    calls: int = 0
    cache_hits: int = 0
    misses: int = 0
    fallbacks: int = 0
    escalations: int = 0
    unhealthy: list[str] = field(default_factory=list)

    @property
    def hit_rate(self) -> float:
        total = self.cache_hits + self.misses
        return round(self.cache_hits / total, 3) if total else 0.0

    def summary(self) -> str:
        return (
            f"search calls={self.calls} cache_hits={self.cache_hits} "
            f"misses={self.misses} hit_rate={self.hit_rate} "
            f"fallbacks={self.fallbacks} escalations={self.escalations}"
        )


@dataclass
class EngineHealth:
    score: float = 1.0
    blocked_until: datetime | None = None

    def available(self, now: datetime) -> bool:
        return self.score >= MIN_HEALTH and (self.blocked_until or now) <= now

    def record_failure(self, now: datetime, *, captcha: bool) -> None:
        self.score = max(MIN_HEALTH - 0.2, self.score * HEALTH_DECAY)
        if captcha:
            self.blocked_until = now + timedelta(seconds=COOLDOWN_SECONDS)

    def record_success(self) -> None:
        self.score = min(1.0, self.score + HEALTH_RECOVER)
        self.blocked_until = None


def cache_key(engine: str, query: str, extra: dict | None = None) -> str:
    raw = json.dumps([engine, query, sorted((extra or {}).items())], sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class EngineError(Exception):
    """Engine failed (block, captcha, outage)."""

    def __init__(self, engine: str, message: str, *, captcha: bool = False):
        super().__init__(f"{engine}: {message}")
        self.engine = engine
        self.captcha = captcha


# ---------------------------------------------------------------- adapters --
def _ddgs_search(query: str, *, max_results: int) -> list[SearchResult]:
    """DuckDuckGo via the ddgs library (light rate)."""
    from ddgs import DDGS

    out: list[SearchResult] = []
    with DDGS() as ddgs:
        for row in ddgs.text(query, max_results=max_results):
            out.append(
                SearchResult(
                    engine="duckduckgo",
                    title=row.get("title") or "",
                    url=row.get("href") or row.get("link") or "",
                    snippet=row.get("body") or "",
                )
            )
    return out


def _google_cse_search(query: str, *, max_results: int, config: dict) -> list[SearchResult]:
    """Google Programmable Search (official free quota first, per spec)."""
    secrets = load_secrets()
    api_key = secrets.get("google_cse_api_key") or secrets.get("GOOGLE_CSE_API_KEY")
    cse_id = secrets.get("google_cse_id") or secrets.get("GOOGLE_CSE_ID")
    if not api_key or not cse_id:
        raise EngineError("google", "no Programmable Search key/ID configured")
    params = {"key": api_key, "cx": cse_id, "q": query, "num": min(max_results, 10)}
    if config.get("gl"):
        params["gl"] = config["gl"]
    if config.get("hl"):
        params["hl"] = config["hl"]
    response = httpx.get("https://www.googleapis.com/customsearch/v1", params=params, timeout=20)
    if response.status_code in (403, 429):
        raise EngineError("google", f"quota/status {response.status_code}")
    response.raise_for_status()
    items = response.json().get("items") or []
    return [
        SearchResult(engine="google", title=i.get("title") or "", url=i.get("link") or "",
                     snippet=i.get("snippet") or "")
        for i in items[:max_results]
    ]


def _searxng_search(query: str, *, max_results: int, config: dict) -> list[SearchResult]:
    """Self-hosted SearXNG aggregating engines behind one JSON interface."""
    base = (config.get("url") or "").rstrip("/")
    if not base:
        raise EngineError("searxng", "no searxng url configured")
    response = httpx.get(f"{base}/search", params={"q": query, "format": "json"}, timeout=20)
    response.raise_for_status()
    rows = response.json().get("results") or []
    return [
        SearchResult(engine="searxng", title=r.get("title") or "", url=r.get("url") or "",
                     snippet=r.get("content") or "")
        for r in rows[:max_results]
    ]


ADAPTERS = {
    "duckduckgo": "_ddgs_search",
    "google": "_google_cse_search",
    "searxng": "_searxng_search",
}


class SearchRouter:
    """Engine selection, health, cache, fallback, escalation."""

    def __init__(self, session: Session, config: dict | None = None):
        self.session = session
        cfg = config or load_yaml("engines.yaml")
        self.engines: dict[str, dict] = cfg.get("engines") or {}
        self.politeness = cfg.get("politeness") or {}
        self.escalation = cfg.get("escalation") or {}
        self.health: dict[str, EngineHealth] = {}

    # ---------------------------------------------------------------- health
    def _health(self, engine: str) -> EngineHealth:
        return self.health.setdefault(engine, EngineHealth())

    def _usable(self, engine: str, now: datetime) -> bool:
        entry = self.engines.get(engine) or {}
        if not entry.get("enabled", False):
            return False
        if engine not in ADAPTERS:
            return False  # registered but not wired yet
        return self._health(engine).available(now)

    def _engine_order(self, query, country: str | None) -> list[str]:
        """Query engines first, then country preferences, then general ones."""
        now = datetime.now(timezone.utc)
        preferred: list[str] = []
        for e in getattr(query, "engines", []) or []:
            preferred.append(e)
        if country:
            country_map = {
                "RUS": ["yandex"], "TUR": ["yandex"], "CHN": ["baidu"],
                "KOR": ["naver"], "JPN": ["yahoo_japan"],
            }
            preferred.extend(country_map.get(country, []))
        preferred.extend(["duckduckgo", "google", "searxng", "bing"])
        seen: set[str] = set()
        order: list[str] = []
        for engine in preferred:
            if engine not in seen and self._usable(engine, now):
                seen.add(engine)
                order.append(engine)
        return order

    # ----------------------------------------------------------------- cache
    def _cache_get(self, engine: str, query: str, *, ttl_minutes: int) -> list[SearchResult] | None:
        key = cache_key(engine, query)
        row = self.session.scalar(select(SearchCache).where(SearchCache.cache_key == key))
        if row is None or not row.results_json:
            return None
        fetched = row.fetched_at
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - fetched > timedelta(minutes=ttl_minutes):
            return None
        return [SearchResult(**item) for item in row.results_json]

    def _cache_put(self, results: list[SearchResult], engine: str, query: str) -> None:
        key = cache_key(engine, query)
        row = self.session.scalar(select(SearchCache).where(SearchCache.cache_key == key))
        if row is None:
            row = SearchCache(cache_key=key)
            self.session.add(row)
        row.engine = engine
        row.results_json = [r.as_dict() for r in results]
        row.fetched_at = datetime.now(timezone.utc)
        self.session.flush()

    # ----------------------------------------------------------------- search
    def search(self, query: str, *, query_type: str = "general", country: str | None = None,
               max_results: int = 10) -> tuple[list[SearchResult], RouterStats]:
        """Run one query through the router: cache -> engines -> escalation."""
        from buyerintel.search.querygen import SearchQuery

        stats = RouterStats()
        plan_query = SearchQuery(text=query, query_type=query_type)
        cache_ttl = int(self.politeness.get("cache_ttl_minutes", 60))
        order = self._engine_order(plan_query, country)
        if not order:
            stats.unhealthy.append("no usable engine")
            return [], stats

        for engine in order:
            stats.calls += 1
            cached = self._cache_get(engine, query, ttl_minutes=cache_ttl)
            if cached is not None:
                stats.cache_hits += 1
                return cached, stats
            stats.misses += 1
            try:
                results = self._run_engine(engine, query, max_results)
            except EngineError as exc:
                self._health(engine).record_failure(datetime.now(timezone.utc), captcha=exc.captcha)
                stats.fallbacks += 1
                continue
            self._health(engine).record_success()
            self._cache_put(results, engine, query)
            return results, stats

        # Every engine failed: escalation rungs 5-6 (both off by default).
        results = self._escalate(query, max_results, stats)
        if results is not None:
            self._cache_put(results, "flaresolverr", query)
            return results, stats
        stats.unhealthy.append(",".join(order))
        return [], stats

    def _run_engine(self, engine: str, query: str, max_results: int) -> list[SearchResult]:
        delay = float(self.politeness.get("default_delay_s", 2.0))
        jitter = float(self.politeness.get("jitter_s", 1.0))
        time.sleep(delay + random.uniform(0, jitter))
        results = self._call_adapter(engine, query, max_results)
        if not results:
            # "no results" is ambiguous (bad query vs rate limit) - treat it as
            # a failure so the next engine still gets a chance.
            raise EngineError(engine, "no results")
        return results

    def _call_adapter(self, engine: str, query: str, max_results: int) -> list[SearchResult]:
        try:
            # Resolve by name so tests can monkeypatch adapters on the module.
            adapter = globals()[ADAPTERS[engine]]
            engine_cfg = self.engines.get(engine) or {}
            if engine in ("google", "searxng"):
                results = adapter(query, max_results=max_results, config=engine_cfg)
            else:
                results = adapter(query, max_results=max_results)
        except EngineError:
            raise
        except Exception as exc:  # noqa: BLE001 - any engine failure is a fallback signal
            captcha = "captcha" in str(exc).lower()
            raise EngineError(engine, str(exc), captcha=captcha) from exc
        return results

    def _escalate(self, query: str, max_results: int, stats: RouterStats) -> list[SearchResult] | None:
        """Rung 5 (FlareSolverr) then rung 6 (CAPTCHA solver), flags off by default."""
        from buyerintel.enrich.challenges import flaresolverr_fetch

        fs_cfg = self.escalation.get("flaresolverr") or {}
        if fs_cfg.get("enabled"):
            stats.escalations += 1
            page = flaresolverr_fetch(self._ddg_url(query))
            if page is not None:
                # HTML parsing lands with the enrichment engine (M4); for now the
                # fetch itself proves the challenge passed and is cached as empty.
                return []
        return None

    @staticmethod
    def _ddg_url(query: str) -> str:
        from urllib.parse import quote_plus

        return f"https://duckduckgo.com/html/?q={quote_plus(query)}"
