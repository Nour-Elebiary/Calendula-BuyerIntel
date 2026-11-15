"""Website crawl for enrichment (spec 12.1).

Crawls a company site: home, about, contact, team, products, catalog,
sourcing/suppliers pages and the sitemap. Detects page language and keeps
original-language text. Every page goes through the fetch escalation ladder
and is cached, so a re-run never refetches.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from sqlalchemy.orm import Session

from buyerintel.config import load_yaml
from buyerintel.enrich.fetch import BlockedError, FetchLadder, FetchResult

LANG_RE = re.compile(r'<html[^>]*\blang=["\']([a-zA-Z-]{2,8})["\']', re.I)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
ANCHOR_RE = re.compile(r'<a[^>]*href=["\']([^"\'#]+)["\']', re.I)
BASE_RE = re.compile(r'<base[^>]*href=["\']([^"\']+)["\']', re.I)


@dataclass
class CrawledPage:
    url: str
    html: str
    language: str | None = None
    title: str = ""
    rung: int = 1
    from_cache: bool = False


@dataclass
class CrawlResult:
    domain: str
    pages: list[CrawledPage] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)   # urls blocked at every rung

    @property
    def text(self) -> str:
        """All page text concatenated (language-tagged titles kept)."""
        return "\n\n".join(p.html for p in self.pages)

    @property
    def languages(self) -> set[str]:
        return {p.language for p in self.pages if p.language}


def detect_language(html: str) -> str | None:
    match = LANG_RE.search(html or "")
    if not match:
        return None
    return match.group(1).lower()[:2] or None


def strip_tags(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _same_host(url: str, host: str) -> bool:
    return urlparse(url).netloc.lower().endswith(host.lower())


def discover_links(html: str, base_url: str, *, host: str, max_links: int = 8) -> list[str]:
    """Same-host links whose path matches any crawl-interest keyword."""
    base = BASE_RE.search(html)
    if base:
        base_url = urljoin(base_url, base.group(1))
    keywords = ("about", "contact", "team", "product", "catalog", "sourc", "supplier")
    found: list[str] = []
    seen: set[str] = set()
    for href in ANCHOR_RE.findall(html or ""):
        absolute = urljoin(base_url, href.strip())
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https") or not _same_host(absolute, host):
            continue
        path = parsed.path.lower().rstrip("/")
        if path in seen or path == "":
            continue
        if any(k in path for k in keywords):
            seen.add(path)
            found.append(f"{parsed.scheme}://{parsed.netloc}{parsed.path}")
        if len(found) >= max_links:
            break
    return found


class SiteCrawler:
    """Crawls the configured pages of one domain through the fetch ladder."""

    def __init__(self, session: Session, config: dict | None = None):
        self.session = session
        self.cfg = config or load_yaml("enrich.yaml")
        self.crawl_cfg = self.cfg.get("crawl") or {}
        self.ladder = FetchLadder(session, config=self.cfg)

    def crawl(self, domain: str, *, max_pages: int | None = None) -> CrawlResult:
        limit = max_pages or int(self.crawl_cfg.get("max_pages", 12))
        result = CrawlResult(domain=domain)
        base = f"https://{domain}"

        # Priority pages from config, then discovered links, then the sitemap.
        urls = [f"{base}{p}" for p in self.crawl_cfg.get("discover_paths") or [""]]
        queue = list(dict.fromkeys(urls))
        visited: set[str] = set()

        while queue and len(result.pages) + len(result.failed) < limit:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            try:
                fetched = self.ladder.fetch(url)
            except BlockedError:
                result.failed.append(url)
                continue
            page = CrawledPage(
                url=url,
                html=fetched.html,
                language=detect_language(fetched.html),
                title=(TITLE_RE.search(fetched.html).group(1).strip()
                       if TITLE_RE.search(fetched.html) else ""),
                rung=fetched.rung,
                from_cache=fetched.from_cache,
            )
            result.pages.append(page)
            # Discover more same-host pages from the home page only.
            if urlparse(url).path in ("", "/"):
                for link in discover_links(fetched.html, url, host=domain):
                    if link not in visited:
                        queue.append(link)

        # Sitemap as a page too (it lists product/catalog urls).
        try:
            sitemap = self.ladder.fetch(f"{base}/sitemap.xml")
            result.pages.append(CrawledPage(url=f"{base}/sitemap.xml", html=sitemap.html,
                                            rung=sitemap.rung, from_cache=sitemap.from_cache))
        except BlockedError:
            pass
        return result
