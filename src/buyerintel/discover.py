"""Self-directed discovery (owner request 2026-11-16: every pipeline step must
be drivable from the UI, including the SEARCH-BY-YOURSELF mode - general
keywords, countries -> their expos and companies, and BOL-record presence -
not only the import buttons).

Three discovery modes, all building on the M3 search router:

1. KEYWORD  - free-text queries ("herbs importer germany"); results become
   CANDIDATE companies (domain + name from the result, full provenance in
   source_record). Re-running is idempotent: one SourceRecord per (source,
   raw_hash=url hash), exactly like the M0 import.
2. COUNTRY  - a country (+ optional product) is expanded into a query plan
   ("{product} importer {country}" x role words x ontology languages) and run
   through the same candidate pipeline; a second plan finds the country's
   EXPOS as leads (stored as source_record rows, source=expo_lead).
3. BOL      - for every known company the US public BOL connector (ImportYeti
   public pages) is queried; companies with a shipment footprint get the
   bol_present tag and their rows are stored as shipments (M7 upsert).

Social/search/news hosts never become companies. Nothing here ever contacts
the candidates - discovery only LISTENS on public pages.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.db.models import Company, CompanyAlias, SourceRecord

# Hosts that are platforms, not companies - a result here is never a lead.
STOP_HOSTS = {
    "linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com",
    "youtube.com", "tiktok.com", "pinterest.com",
    "google.com", "bing.com", "duckduckgo.com", "yandex.com", "yandex.ru",
    "baidu.com", "naver.com", "yahoo.com",
    "wikipedia.org", "reddit.com", "quora.com", "medium.com",
    "glassdoor.com", "indeed.com", "crunchbase.com", "bloomberg.com",
    "github.com", "stackoverflow.com", "amazon.com", "ebay.com",
    "europa.eu", "gov.uk",
    # B2B directories / aggregators: a listing page is not a buyer (found live
    # 2026-11-16: ensun/trademo/europages pages swallowed real-company slots)
    "ensun.io", "trademo.com", "europages.co.uk", "europages.com", "europages.de",
    "go4worldbusiness.com", "alibaba.com", "aliexpress.com",
    "made-in-china.com", "dhgate.com", "tradeindia.com", "tradekey.com",
    "exporthub.com", "kompass.com", "thomasnet.com", "zoominfo.com", "dnb.com",
}

# Titles that mean "no real company name in the result".
_JUNK_TITLES = {"home", "welcome", "home page", "start", "index", "contact",
                "about us", "impressum", "products", "not found"}

# Generic category words that are NOT company names ("Herbs - JGS" -> JGS).
GENERIC_WORDS = {"herbs", "herb", "spices", "spice", "tea", "teas",
                 "botanicals", "ingredients", "food", "foods", "products"}

TITLE_SEPARATORS = re.compile(r"\s+(?:[|]|\-{1,2}|\u2013|\u2014|::|\u00bb)\s+")


# ------------------------------------------------------------------ helpers
def domain_of(url: str) -> str | None:
    """Registerable domain of a result URL (www stripped, no path)."""
    try:
        host = (urlparse(url).hostname or "").lower().strip(".")
    except ValueError:
        return None
    if not host or "." not in host:
        return None
    return host.removeprefix("www.")


LEGAL_FORM_RE = re.compile(
    r"\b(?:gmbh|gmbh & co|ag|ltd|limited|llc|inc|corp|b\.v\.|bv|nv|oy|aps|ab|"
    r"sa|s\.a\.|sas|srl|s\.r\.l\.|kg|ohg|ug|pte|pty|jsc|s\.r\.o\.|d\.o\.o\.|"
    r"kft|zrt|a\.s\.|co)\b\.?", re.I)


def _product_words(session: Session | None) -> set[str]:
    """Ontology product names (lowercase) as a blocking set; empty on error."""
    if session is None:
        return set()
    try:
        from buyerintel.db.models import ProductName

        return {row.lower() for row in session.scalars(
            select(ProductName.name)).all()}
    except Exception:  # noqa: BLE001 - naming heuristic only
        return set()


def name_from_result(title: str, url: str, session: Session | None = None) -> str:
    """Best-effort company name from a result title.

    Segment priority (each title split on common separators):
    1. the segment carrying a LEGAL-FORM token ("Spices & Herbs - Mueggenburg
       ... GmbH & Co" -> the GmbH segment; found live 2026-11-16),
    2. otherwise, when `session` is given and the first segment is an ontology
       PRODUCT word ("Herbs - JGS" -> "JGS"),
    3. otherwise the first segment ("Acme GmbH - herbs importer" -> Acme GmbH),
    4. domain-label fallback when nothing usable remains.
    """
    title = re.sub(r"\s+", " ", (title or "").strip())
    segments = [s.strip(" -\u2013\u2014|") for s in TITLE_SEPARATORS.split(title)]
    segment = segments[0] if segments else title
    segment = re.sub(r"^(?:home|welcome)\s*[|:-]\s*", "", segment,
                     flags=re.I).strip()

    # 1. a segment with a legal-form token is almost always the company
    legal = [s for s in segments if LEGAL_FORM_RE.search(s or "")]
    if legal:
        return max(legal, key=len).rstrip(" .")

    # 2. first segment is a generic/product word -> prefer a different segment
    if segments and segment.lower() in (GENERIC_WORDS | _product_words(session)):
        alt = segments[-1]
        if (alt and alt.lower() not in _JUNK_TITLES
                and alt.lower() != segment.lower()):
            return alt

    if (not segment or segment.lower() in _JUNK_TITLES
            or len(segment) < 3
            or ("." in segment and " " not in segment
                and domain_of(f"http://{segment}"))):
        dom = domain_of(url)
        if dom:
            base = dom.split(".")[0]
            return base.replace("-", " ").replace("_", " ").title()
    return segment


def find_existing(session: Session, *, name: str, domain: str | None) -> Company | None:
    """Match a candidate against the DB: by domain, then name, then alias."""
    if domain:
        rows = session.scalars(select(Company).where(Company.domain.is_not(None))).all()
        for c in rows:
            if (c.domain or "").lower().removeprefix("www.") == domain:
                return c
    if name:
        hit = session.scalar(select(Company).where(
            Company.canonical_name.ilike(name)))
        if hit is not None:
            return hit
        alias_rows = session.execute(
            select(CompanyAlias.alias, CompanyAlias.company_id)
            .where(CompanyAlias.alias.ilike(name))).all()
        if alias_rows:
            cid = alias_rows[0][1]
            return session.get(Company, cid)
    return None


def _record_hash(result) -> str:
    return hashlib.sha256((result.url or "").strip().lower().encode("utf-8")).hexdigest()


def candidate_from_result(session: Session, result, *, tag: str,
                          country: str | None = None,
                          source: str = "search") -> tuple[Company | None, str]:
    """Turn one search result into a candidate company.

    Returns (company_or_None, status) with status in new|existing|skipped.
    Idempotent: the SourceRecord unique key is (source, hash of url), so
    re-running the same search never duplicates anything.
    """
    dom = domain_of(result.url or "")
    host = (urlparse(result.url or "").hostname or "").lower().removeprefix("www.")
    if host in STOP_HOSTS or dom is None:
        return None, "skipped"
    if session.scalar(select(SourceRecord.raw_hash).where(
            SourceRecord.source == source,
            SourceRecord.raw_hash == _record_hash(result))) is not None:
        return None, "skipped"   # this URL was already ingested before
    name = name_from_result(result.title or "", result.url, session=session)
    existing = find_existing(session, name=name, domain=dom)
    record = SourceRecord(source=source, raw_hash=_record_hash(result),
                          raw_json={"title": result.title, "url": result.url,
                                    "engine": result.engine,
                                    "snippet": (result.snippet or "")[:400],
                                    "tag": tag})
    if existing is not None:
        record.company_id = existing.id
        session.add(record)
        return existing, "existing"
    company = Company(canonical_name=name, domain=dom,
                      country=country, tags=[tag], confidence=0.4)
    session.add(company)
    session.flush()
    record.company_id = company.id
    session.add(record)
    session.add(CompanyAlias(company_id=company.id, alias=(result.title or name)[:255],
                             kind="search"))
    return company, "new"


# ------------------------------------------------------------------ keyword
def search_candidates(session: Session, queries: list[str], *, country: str | None = None,
                      tag: str = "search", max_results: int = 8,
                      router=None) -> dict:
    """Run queries through the search router and build candidate companies.

    `router` is injectable (tests pass a stub); the default builds the real
    M3 SearchRouter with its cache, health and fallback behaviour.
    """
    if router is None:
        from buyerintel.search.router import SearchRouter

        router = SearchRouter(session)
    created: list[dict] = []
    existing: list[dict] = []
    skipped = 0
    stats = None
    for query in queries:
        query = (query or "").strip()
        if not query:
            continue
        results, stats = router.search(query, country=country, max_results=max_results)
        for result in results:
            company, status = candidate_from_result(
                session, result, tag=tag, country=country)
            if status == "new":
                created.append({"name": company.canonical_name,
                                "domain": company.domain,
                                "title": result.title,
                                "url": result.url, "query": query})
            elif status == "existing":
                existing.append({"name": company.canonical_name, "url": result.url,
                                 "query": query})
            else:
                skipped += 1
    session.flush()
    return {"created": created, "existing": existing, "skipped": skipped,
            "queries_run": len(queries),
            "stats": stats.summary() if stats is not None else "no queries"}


# ------------------------------------------------------------------ country
COUNTRY_ROLES = ("importer", "wholesale", "distributor", "buyer", "organic")


def country_queries(country: str, *, product: str | None = None,
                    product_terms: list[str] | None = None) -> list[str]:
    """Query plan for a country: role words x product terms (owner's example:
    'herbs importer germany')."""
    country = (country or "").strip()
    base = (product or "herbs").strip() or "herbs"
    terms = [base] + [t for t in (product_terms or []) if t and t != base]
    out: list[str] = []
    seen: set[str] = set()
    for term in terms:
        for role in COUNTRY_ROLES:
            text = f"{term} {role} {country}".strip()
            if text.lower() not in seen:
                seen.add(text.lower())
                out.append(text)
    return out


def expo_queries(country: str, *, product: str | None = None) -> list[str]:
    """Find the country's expos as leads."""
    country = (country or "").strip()
    base = (product or "herbs").strip()
    return [f"{country} {base} trade expo".strip(),
            f"{country} {base} trade fair".strip(),
            f"{country} food ingredients exhibition".strip()]


def expo_leads(session: Session, queries: list[str], *, max_results: int = 8,
               router=None) -> list[dict]:
    """Expo-discovery results stored as lead records (source=expo_lead).

    Expos are not companies: they are recorded with their URL so the user can
    decide to ingest a structured exhibitor list through the existing
    ingest-expo path (config/expo_maps YAML + CSV) later.
    """
    if router is None:
        from buyerintel.search.router import SearchRouter

        router = SearchRouter(session)
    known = set(session.scalars(select(SourceRecord.raw_hash).where(
        SourceRecord.source == "expo_lead")).all())
    out: list[dict] = []
    for query in queries:
        query = (query or "").strip()
        if not query:
            continue
        results, _stats = router.search(query, max_results=max_results)
        for result in results:
            h = hashlib.sha256((result.url or "").strip().lower().encode("utf-8")).hexdigest()
            if h in known:
                out.append({"title": result.title, "url": result.url,
                            "query": query, "stored": "already"})
                continue
            known.add(h)
            session.add(SourceRecord(
                source="expo_lead", raw_hash=h,
                raw_json={"title": result.title, "url": result.url,
                          "engine": result.engine, "query": query,
                          "snippet": (result.snippet or "")[:400]}))
            out.append({"title": result.title, "url": result.url,
                        "query": query, "stored": "yes"})
    session.flush()
    return out


# ------------------------------------------------------------------ BOL
def bol_check(session: Session, companies: list, *, source=None) -> list[dict]:
    """Check each company's public BOL footprint (US records).

    Uses a ShipmentSource (default: ImportYeti public pages). A company with
    rows gets the `bol_present` tag and its shipment rows are stored via the
    source's own run_for_company (fetch -> normalize -> idempotent upsert).
    """
    if source is None:
        from buyerintel.shipments.us_bol import ImportYetiPublicSource

        source = ImportYetiPublicSource(session)
    out: list[dict] = []
    for company in companies:
        result = source.run_for_company(company)
        rows = result.get("rows", 0)
        if rows and result.get("error") is None:
            tags = list(company.tags or [])
            if "bol_present" not in tags:
                tags.append("bol_present")
                company.tags = tags
        out.append({"company_id": company.id, "name": company.canonical_name,
                    "rows": rows, "created": result.get("created", 0),
                    "error": result.get("error")})
    session.flush()
    return out


def run_bol_queue(session: Session, *, tier: int | None = 1,
                  limit: int | None = None, source=None) -> list[dict]:
    """BOL check over the company queue (default: Tier 1, spec S6 scope)."""
    query = select(Company).where(Company.state == "active")
    if tier is not None:
        query = query.where(Company.tier == tier)
    companies = session.scalars(query.order_by(Company.id)).all()
    if limit is not None:
        companies = companies[:limit]
    return bol_check(session, companies, source=source)
