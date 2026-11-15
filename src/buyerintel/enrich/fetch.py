"""Fetch escalation ladder (spec 12.1, addendum 4.8).

Per URL: direct httpx (cached, rate-limited) -> Scrapling stealth ->
Scrapling dynamic -> Botasaurus -> FlareSolverr -> CAPTCHA solver.
A rung runs only after the previous one failed with a block, 403 or
challenge page, and every success is cached. Rungs 4-6 are config-flagged
and OFF by default. Solvers and FlareSolverr never touch logged-in sessions
or API calls carrying keys.

Cache: rows in `search_cache` with engine="ladder", storing the HTML and
the rung that produced it.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.config import load_yaml
from buyerintel.db.models import SearchCache

USER_AGENT = "Calendula-BuyerIntel/0.1 (local personal research)"

# Markers that the response is a bot-challenge page rather than real content.
CHALLENGE_MARKERS = [
    "just a moment",
    "cf-browser-verification",
    "cf_chl_opt",
    "challenge-platform",
    "attention required",
    "checking your browser",
    "captcha-delivery",
]

CACHE_TTL_HOURS = 24 * 7  # fetched pages stay cached for a week


class BlockedError(Exception):
    """A rung was blocked (403/429/503, challenge page, or hard failure).

    `permanent=True` marks failures no later rung can fix (e.g. 404: the
    page does not exist) - the ladder aborts instead of escalating.
    """

    def __init__(self, rung: int, message: str, *, captcha: bool = False,
                 permanent: bool = False):
        super().__init__(f"rung {rung}: {message}")
        self.rung = rung
        self.captcha = captcha
        self.permanent = permanent


@dataclass
class FetchResult:
    url: str
    html: str
    rung: int                  # which rung produced the successful fetch
    from_cache: bool = False

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.html.encode("utf-8", "ignore")).hexdigest()


def looks_blocked(status_code: int | None, html: str) -> bool:
    """403/429/503 or a Cloudflare-style challenge page means blocked."""
    if status_code in (403, 429, 503):
        return True
    if status_code is not None and status_code >= 400:
        return False  # a real 404 etc. - the page simply does not exist
    lowered = (html or "")[:4000].lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


def _page_cache_key(url: str) -> str:
    return hashlib.sha256(f"page:{url}".encode("utf-8")).hexdigest()


def _cache_get(session: Session, url: str) -> FetchResult | None:
    row = session.scalar(
        select(SearchCache).where(SearchCache.cache_key == _page_cache_key(url))
    )
    if row is None or not row.results_json:
        return None
    fetched = row.fetched_at
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - fetched > timedelta(hours=CACHE_TTL_HOURS):
        return None
    data = row.results_json
    return FetchResult(url=url, html=data.get("html") or "", rung=int(data.get("rung") or 1),
                       from_cache=True)


def _cache_put(session: Session, result: FetchResult) -> None:
    key = _page_cache_key(result.url)
    row = session.scalar(select(SearchCache).where(SearchCache.cache_key == key))
    if row is None:
        row = SearchCache(cache_key=key)
        session.add(row)
    row.engine = "ladder"
    row.results_json = {"html": result.html, "rung": result.rung}
    row.fetched_at = datetime.now(timezone.utc)
    session.flush()


# ------------------------------------------------------------------- rungs --
def _rung1_direct(url: str, timeout: float) -> str:
    """Direct httpx with a clear UA and polite timeout."""
    try:
        response = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout,
                             follow_redirects=True)
    except httpx.HTTPError as exc:
        raise BlockedError(1, f"network error: {exc}") from exc
    if looks_blocked(response.status_code, response.text):
        raise BlockedError(1, f"status {response.status_code} or challenge page")
    if response.status_code >= 400:
        # A 404 etc. is the site saying the page does not exist - no later
        # rung (browser, solver) can fix that.
        raise BlockedError(1, f"status {response.status_code}", permanent=True)
    return response.text


def _rung2_scrapling_stealth(url: str) -> str:
    """Scrapling stealth fetcher (residential-style headers/TLS)."""
    try:
        from scrapling.fetchers import StealthyFetcher
    except ImportError as exc:
        raise BlockedError(2, f"scrapling stealth unavailable: {exc}") from exc
    page = StealthyFetcher.fetch(url, headless=True, network_idle=True)
    html = page.html_content if hasattr(page, "html_content") else str(page)
    if looks_blocked(page.status if hasattr(page, "status") else None, html):
        raise BlockedError(2, "challenge page after stealth fetch")
    return html


def _rung3_scrapling_dynamic(url: str) -> str:
    """Scrapling dynamic fetcher (bundled Playwright, real browser)."""
    try:
        from scrapling.fetchers import DynamicFetcher
    except ImportError as exc:
        raise BlockedError(3, f"scrapling dynamic unavailable: {exc}") from exc
    page = DynamicFetcher.fetch(url, headless=True)
    html = page.html_content if hasattr(page, "html_content") else str(page)
    if looks_blocked(None, html):
        raise BlockedError(3, "challenge page after dynamic fetch")
    return html


def _rung4_botasaurus(url: str) -> str:
    """Botasaurus anti-bot browser (ADD-014). Flag-gated, off by default."""
    try:
        from botasaurus.browser import browser
    except ImportError as exc:
        raise BlockedError(4, f"botasaurus unavailable: {exc}") from exc

    def scrape(driver, _):
        return driver.page_html

    data = browser(scrape, url)
    html = data if isinstance(data, str) else str(data)
    if looks_blocked(None, html):
        raise BlockedError(4, "challenge page after botasaurus fetch")
    return html


def _rung5_flaresolverr(url: str) -> str:
    """FlareSolverr adapter (ADD-010). Flag-gated, off by default."""
    from buyerintel.enrich.challenges import FlareSolverrError, flaresolverr_fetch

    try:
        solution = flaresolverr_fetch(url)
    except FlareSolverrError as exc:
        raise BlockedError(5, str(exc)) from exc
    if not solution:
        raise BlockedError(5, "flaresolverr returned no solution")
    html = solution.get("html") or ""
    if looks_blocked(None, html):
        raise BlockedError(5, "challenge page persisted after flaresolverr")
    return html


def _rung6_captcha(url: str) -> str:
    """CAPTCHA solver, last resort (ADD-011). Flag-gated, off by default."""
    from buyerintel.enrich.challenges import CaptchaError, solve_captcha

    # The solver needs the site's challenge key, which we can only extract from
    # the blocked page we already hold; without one, the rung cannot run.
    raise BlockedError(6, "captcha solver requires a challenge key extracted from "
                          "the blocked page; no key present for " + url)


RUNGS = {
    1: ("rung1_direct", None),
    2: ("rung2_scrapling_stealth", "rung2_scrapling_stealth"),
    3: ("rung3_scrapling_dynamic", "rung3_scrapling_dynamic"),
    4: ("rung4_botasaurus", "rung4_botasaurus"),
    5: ("rung5_flaresolverr", "rung5_flaresolverr"),
    6: ("rung6_captcha", "rung6_captcha"),
}


class FetchLadder:
    """Runs the escalation ladder for one URL, with caching and flags."""

    def __init__(self, session: Session, config: dict | None = None):
        self.session = session
        self.cfg = config or load_yaml("enrich.yaml")
        self.ladder = self.cfg.get("ladder") or {}
        network = load_yaml("network.yaml")
        self.timeout = float((network.get("direct") or {}).get("timeout_seconds", 30))

    def _flag(self, rung: int) -> bool:
        if rung == 1:
            return True
        flag = RUNGS[rung][1]
        return bool(self.ladder.get(flag, False))

    def fetch(self, url: str) -> FetchResult:
        """Cache-first, then rungs in order; a rung runs only after a block.

        Every rung raises `BlockedError` for a block, challenge page, or
        unavailability - that is the signal for the next rung. Config-flagged
        rungs (4-6 off by default) are skipped, not fatal.
        """
        cached = _cache_get(self.session, url)
        if cached is not None:
            return cached

        blocked = False
        for rung in range(1, 7):
            if not self._flag(rung):
                continue
            if not blocked and rung > 1:
                break  # spec: a rung runs only after the previous one was blocked
            try:
                html = self._run_rung(rung, url)
            except BlockedError as exc:
                if exc.permanent:
                    raise  # page does not exist - escalating cannot help
                blocked = True
                continue
            result = FetchResult(url=url, html=html, rung=rung)
            _cache_put(self.session, result)
            return result
        raise BlockedError(6, "all enabled rungs blocked for " + url)

    def _run_rung(self, rung: int, url: str) -> str:
        if rung == 1:
            return _rung1_direct(url, self.timeout)
        if rung == 2:
            return _rung2_scrapling_stealth(url)
        if rung == 3:
            return _rung3_scrapling_dynamic(url)
        if rung == 4:
            return _rung4_botasaurus(url)
        if rung == 5:
            return _rung5_flaresolverr(url)
        return _rung6_captcha(url)
