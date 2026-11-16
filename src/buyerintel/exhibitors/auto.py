"""AUTO DATA-MINING of exhibitor lists (owner request 2026-11-16).

"it should also detect - on its own - the correct exhibitor list page and
the right filters (whether the list is an embedded component or the
original page and how it'll work it out, whether using selenium and
chromium with scenarios or using parameters or what)".

So given an expo landing page the miner works out, by itself:

1. WHICH PAGE is the exhibitor list (anchor/iframe/embed detection).
2. HOW the list is rendered - a STRATEGY LADDER tried in order:
   static    rows readable straight from HTML (profile links, tables)
   json      rows inside an embedded payload (__NEXT_DATA__, __NUXT__,
             ld+json ItemList, in-page data endpoints)
   params    the list is a component fed by URL parameters -> the miner
             crafts parameter URLs (?page=2, ?exhibitors=1, common
             directory params) and re-extracts per page
   dynamic   nothing rendered -> headless Chromium (Selenium, Chrome or
             Edge) with a generic load/scroll/load-more scenario, then
             the same extractors run on the final DOM. Degrades with a
             clear message when Selenium is absent.
3. Extraction feeds the EXISTING importer (temp CSV + generated mapping
   YAML), so ingestion stays idempotent per (source, row hash) and expo
   appearances land exactly like a manual ingest.

Every extraction step is fixture-testable; `fetch` is injectable so tests
never touch the network.
"""
from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urljoin, urlencode, parse_qsl, urlunparse

from buyerintel.connectors.expos import create_appearances
from buyerintel.connectors.importer import import_workbook
from buyerintel.exhibitors.dynamic import DynamicUnavailable

# Anchor text / href words that mark the exhibitor list page.
LIST_PAGE_RE = re.compile(
    r"exhibitor|participant|companies?\b|buyer|supplier|brand\s*list|"
    r"directory| attending |who.?s (?:coming|exhibiting)|aussteller|"
    r"exposant", re.I)

# Hrefs of the ACTUAL list ("Exhibitors & Products", "Find exhibitors").
_GOOD_HREF_RE = re.compile(
    r"exhibitors?-?(?:products|list|directory|companies|find)|"
    r"exhibitor-list|participants|aussteller", re.I)

# Marketing pages ABOUT exhibiting - never the list itself (found live
# 2026-11-16 on biofach.de: the chatbot and booking links outscored the
# real "Exhibitors & Products" page without this filter).
_BAD_HREF_RE = re.compile(
    r"become|book-your-stand|book-your|apply|chatbot|register|"
    r"sign-?up|tickets|marketing", re.I)

# Href patterns of a company profile inside a list.
PROFILE_HREF_RE = re.compile(
    r"/(?:exhibitor|company|participant|supplier|member|profile|brand|buyer|listing)"
    r"(?:/|[/?=])", re.I)

# Nav / footer junk that must not become a company row.
_ROW_JUNK_RE = re.compile(
    r"^(?:all|view all|more|see more|exhibitors?\s*\d{4}|register|tickets?|"
    r"apply|contact|login|sign ?up|menu|home|about|news|press|travel|"
    r"venue|program|agenda|site|website|visit(?:\s+\w+)?|click here|"
    r"read more|details|more info|homepage|link|chat|ask|search|filter|"
    r"editor|sitemap|teaser|metadata|navigation|footer|sidebar|header|"
    r"language[a-z]{2}|english|german)\b", re.I)

NAME_KEYS = ("name", "exhibitorname", "exhibitor_name", "company",
             "companyname", "company_name", "title")
COUNTRY_KEYS = ("country", "countryname", "country_name", "region")
CITY_KEYS = ("city", "town", "location")
WEBSITE_KEYS = ("website", "url", "homepage", "web", "domain", "site")

MAX_ROWS = 5000   # safety stop for runaway extraction


# ------------------------------------------------------------------ detect
def find_list_pages(html: str, base_url: str) -> list[dict]:
    """Candidate exhibitor-list pages found in `html`, best first.

    Looks at anchors (text + href) and embedded sources (iframes,
    data-url attributes) - both the original page and embedded components
    are detected. Candidates resolve against base_url and carry a score.
    """
    out: dict[str, dict] = {}

    def add(href: str, text: str, kind: str, score: int) -> None:
        href = urljoin(base_url or "", href.strip())
        if not href.startswith("http"):
            return
        if href in out:
            out[href]["score"] += score
            if text and text not in out[href]["text"]:
                out[href]["text"] = f"{out[href]['text']} | {text}"
        else:
            out[href] = {"url": href, "text": text, "kind": kind,
                         "score": score}

    for m in re.finditer(
            r"<a\b[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>",
            html or "", re.I | re.S):
        href, inner = m.group(1), m.group(2)
        text = re.sub(r"<[^>]+>", " ", inner)
        text = re.sub(r"\s+", " ", text).strip()
        hay = f"{text} {href}"
        if not LIST_PAGE_RE.search(hay):
            continue
        if _BAD_HREF_RE.search(hay):
            continue   # "Become exhibitor", chatbot, booking - not the list
        score = 1
        if LIST_PAGE_RE.search(text or ""):
            score += 2
        if re.search(r"exhibitor", hay, re.I):
            score += 2
        if _GOOD_HREF_RE.search(href or ""):
            score += 4
        add(href, text, "link", score)

    for m in re.finditer(
            r"<iframe\b[^>]*\bsrc=[\"']([^\"'#]+)[\"']", html or "", re.I):
        add(m.group(1), "embedded component (iframe)", "embed", 5)
    for m in re.finditer(
            r"data-(?:url|src|list)[\"']?=[\"']([^\"'#]+)[\"']",
            html or "", re.I):
        add(m.group(1), "embedded component (data-url)", "embed", 5)

    return sorted(out.values(), key=lambda c: -c["score"])


def detect_embeds(html: str) -> dict:
    """How the page renders its list: embedded payloads and data endpoints."""
    h = html or ""
    embeds = {
        "next_data": "__NEXT_DATA__" in h,
        "nuxt": "__NUXT__" in h or "window.__NUXT__" in h,
        "ldjson": 'type="application/ld+json"' in h
                  or "type='application/ld+json'" in h,
        "api_endpoints": sorted(set(re.findall(
            r"[\"'](/(?:api|data)/[a-z0-9_\-/]*(?:exhibitor|company|participant)"
            r"[a-z0-9_\-/]*)[\"']", h, re.I))),
        "iframes": [m.group(1) for m in re.finditer(
            r"<iframe\b[^>]*\bsrc=[\"']([^\"'#]+)[\"']", h, re.I)],
    }
    return embeds


# ------------------------------------------------------------------ extract
def _clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").replace("&amp;", "&")
                  .replace("&#39;", "'").replace("&quot;", '"')
                  .replace('"', "")).strip()
    return text


def _xpath_text(el) -> str:
    """Full text under `el`; scrapling's xpath('string(.)') returns a LIST
    of text nodes, so normalize to one string. Line structure is kept -
    callers clean/split on lines themselves."""
    try:
        raw = el.xpath("string(.)")
    except Exception:  # noqa: BLE001
        raw = el.text or ""
    if isinstance(raw, (list, tuple)):
        raw = "\n".join(str(part) for part in raw)
    return (str(raw) or "").replace("&amp;", "&").replace("&#39;", "'") \
        .replace("&quot;", '"')


def _row_ok(name: str) -> bool:
    if not name or len(name) < 3 or len(name) > 120:
        return False
    if _ROW_JUNK_RE.match(name):
        return False
    if name.lower() in {"exhibitors", "exhibitor list", "companies",
                        "participants", "exhibitor"}:
        return False
    return True


def exhibitors_from_static(html: str, base_url: str = "") -> list[dict]:
    """Rows readable straight from the HTML: links to company profiles.
    Deliberately NARROW: every external anchor (partner/press lists) must
    NOT become a row - on biofach.de that heuristic harvested 29 media
    partners instead of exhibitors. External-site cards are handled by
    exhibitors_from_cards, which validates card structure first."""
    rows: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(
            r"<a\b[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>",
            html or "", re.I | re.S):
        href, inner = m.group(1), m.group(2)
        text = _clean_text(re.sub(r"<[^>]+>", " ", inner))
        if not _row_ok(text):
            continue
        if not PROFILE_HREF_RE.search(href or ""):
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        rows.append({"name": text})
        if len(rows) >= MAX_ROWS:
            break
    return rows


def exhibitors_from_cards(html: str, base_url: str = "") -> list[dict]:
    """Card/structured-list extraction via Scrapling's adaptive parser
    (community patterns): profile anchors first, then repeated card
    containers grouped by class token, then `find_similar` element
    grouping. Catches nested markup and card layouts the regex pass
    misses. Returns [] when scrapling's parser cannot help - never raises.
    """
    try:
        from scrapling.parser import Selector
    except Exception:  # noqa: BLE001 - scrapling missing: degrade to regex
        return []
    try:
        page = Selector(html or "")
    except Exception:  # noqa: BLE001 - malformed html
        return []

    page_host = ""
    try:
        page_host = (urlparse(base_url).hostname or "").lower()
    except ValueError:
        pass
    rows: list[dict] = []
    seen: set[str] = set()

    def add(name: str, website: str | None = None) -> None:
        if not _row_ok(name):
            return
        key = name.lower()
        if key in seen:
            return
        seen.add(key)
        row = {"name": name}
        if website:
            row["website"] = website
        rows.append(row)

    def site_of(href: str) -> str | None:
        full = urljoin(base_url, href or "")
        try:
            host = (urlparse(full).hostname or "").lower()
        except ValueError:
            return None
        if host and page_host and host != page_host \
                and host not in ("gstatic.com", "cloudfront.net"):
            return host.removeprefix("www.")
        return None

    # 1) anchors with profile-like hrefs - full text via XPath string()
    for a in page.css("a[href]"):
        href = a.attrib.get("href", "")
        if not PROFILE_HREF_RE.search(href or ""):
            continue
        add(_clean_text(_xpath_text(a)), site_of(href))

    # 2) repeated card containers by class token
    if len(rows) < 3:
        from collections import Counter

        tokens = Counter()
        for el in page.css("[class]"):
            for token in (el.attrib.get("class") or "").lower().split():
                if re.search(r"exhibitor|card|item|entry|participant|"
                             r"company|brand", token):
                    tokens[token] += 1
        for token, count in tokens.most_common(3):
            if count < 3:
                continue
            group_rows: list[dict] = []
            try:
                group = page.css(f"[class*='{token}']")
            except Exception:  # noqa: BLE001 - cssselect quirk
                continue
            for el in group:
                raw = _xpath_text(el)
                name = next((_clean_text(ln) for ln in raw.splitlines()
                             if _row_ok(_clean_text(ln))), None)
                website = None
                linked = False
                try:
                    for a in el.css("a[href]"):
                        ahref = a.attrib.get("href", "")
                        if PROFILE_HREF_RE.search(ahref):
                            linked = True
                        site = site_of(ahref)
                        if site:
                            linked = True
                            if website is None:
                                website = site
                except Exception:  # noqa: BLE001
                    pass
                if name:
                    group_rows.append({"name": name, "website": website,
                                       "linked": linked})
            # A card group must LOOK like a directory: cards that link to
            # company profiles or company sites. Class tokens on plain
            # navigation ("navigation-list-item") link to internal pages -
            # not a directory, rejected.
            linked_rows = [r for r in group_rows if r["linked"]]
            if len({r["name"].lower() for r in linked_rows}) >= 3:
                for row in linked_rows:
                    add(row["name"], row.get("website"))
                break

    # 3) find_similar grouping (scrapling's adaptive feature): group the
    # first profile anchor's siblings when the above found too little
    if len(rows) < 3:
        anchors = [a for a in page.css("a[href]")
                   if PROFILE_HREF_RE.search(a.attrib.get("href", ""))]
        if anchors:
            try:
                similar = [anchors[0]] + list(anchors[0].find_similar() or [])
                for el in similar:
                    add(_clean_text(_xpath_text(el)),
                        site_of(el.attrib.get("href", "")))
            except Exception:  # noqa: BLE001 - parser quirk, not fatal
                pass
    return rows


def _pick_key(node: dict, keys: tuple) -> str | None:
    """Value of the first matching key, case-insensitively (payload keys
    arrive as companyName, Country, boothNumber, ...)."""
    lowered = {k.lower(): v for k, v in node.items()}
    for key in keys:
        if key in lowered:
            return lowered[key]
    return None


def exhibitors_from_json(html: str) -> list[dict]:
    """Rows inside embedded JSON payloads: __NEXT_DATA__, __NUXT__,
    ld+json ItemList. STRICT walk: a dict counts as a company row only
    with a name-ish key AND a structured company signal (country/city/
    website/booth/...) - CMS payloads (Sitecore etc.) carry hundreds of
    name-bearing navigation/style dicts that must not become rows."""
    rows: list[dict] = []
    seen: set[str] = set()

    def walk(node) -> None:
        if len(rows) >= MAX_ROWS:
            return
        if isinstance(node, dict):
            name = None
            for key in NAME_KEYS:
                val = _pick_key(node, (key,))
                if isinstance(val, str):
                    name = _clean_text(val)
                    break
            signal = None
            for skey in COMPANY_SIGNAL_KEYS:
                val = _pick_key(node, (skey,))
                if isinstance(val, str) and val.strip():
                    signal = val
                    break
                if isinstance(val, (list, dict)) and val:
                    signal = skey   # e.g. booth: [{...}] / products: [...]
                    break
            if _row_ok(name) and signal is not None:
                row = {"name": name}
                for ckey in COUNTRY_KEYS:
                    val = _pick_key(node, (ckey,))
                    if isinstance(val, str) and val.strip():
                        row["country"] = _clean_text(val)
                        break
                for citykey in CITY_KEYS:
                    val = _pick_key(node, (citykey,))
                    if isinstance(val, str) and val.strip():
                        row["city"] = _clean_text(val)
                        break
                for wkey in WEBSITE_KEYS:
                    val = _pick_key(node, (wkey,))
                    if isinstance(val, str) and val.strip().startswith("http"):
                        row["website"] = _clean_text(val)
                        break
                # payload entries repeat; dedupe on name
                if name.lower() not in seen:
                    seen.add(name.lower())
                    rows.append(row)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    try:
        stripped = (html or "").lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            # raw JSON (captured XHR payload, not DOM-embedded)
            walk(json.loads(stripped))
            return rows
    except ValueError:
        return rows
    for m in re.finditer(
            r"<script[^>]*id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>",
            html or "", re.S):
        try:
            walk(json.loads(m.group(1)))
        except ValueError:
            continue
    for m in re.finditer(
            r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
            html or "", re.S):
        try:
            data = json.loads(m.group(1))
            if isinstance(data, dict) and "itemListElement" in data:
                for el in data["itemListElement"]:
                    item = el.get("item", el) if isinstance(el, dict) else {}
                    name = item.get("name") or el.get("name")
                    name = _clean_text(name or "")
                    if _row_ok(name) and name.lower() not in seen:
                        seen.add(name.lower())
                        rows.append({"name": name})
        except ValueError:
            continue
    for m in re.finditer(
            r"window\.__NUXT__\s*=\s*(\{.*?\});", html or "", re.S):
        # Nuxt payloads are JS-ish; JSON rarely parses - try, degrade quietly
        try:
            walk(json.loads(m.group(1)))
        except ValueError:
            continue
    return rows


# Structured signals that mark a REAL company record inside a JSON
# payload (Algolia/GraphQL/API hits) - CMS navigation junk never has these.
COMPANY_SIGNAL_KEYS = ("country", "countryname", "country_name", "city",
                       "town", "address", "booth", "boothnumber",
                       "booth_number", "boothhall", "products", "phone",
                       "telephone", "email", "logo", "logoimageurl",
                       "description", "slogan", "website", "domain",
                       "homepage", "halloffset", "stand")


def exhibitors_from_payload_json(text: str) -> list[dict]:
    """Company rows inside XHR JSON payloads captured during the dynamic
    render (Algolia/GraphQL/API responses - found live 2026-11-16:
    biofach.de's exhibitor widget serves its list from an Algolia POST).

    Same STRICT walk as exhibitors_from_json; kept as a named entry point
    because the two evolve independently (DOM-embedded vs XHR-captured).
    """
    return exhibitors_from_json(text)


# ------------------------------------------------------------------ params
def param_variants(url: str, page: int) -> list[str]:
    """Parameter-crafted variants of `url` for pagination / list filters
    (the 'using parameters' strategy): ?page=N first, then common
    directory-widget params."""
    from urllib.parse import urlencode as _enc

    base = url.split("#", 1)[0]
    sep = "&" if "?" in base else "?"
    return [
        f"{base}{sep}page={page}",
        f"{base}{sep}{_enc({'page': page, 'exhibitors': 1})}",
        f"{base}{sep}{_enc({'pg': page, 'exhibitor': 1})}",
    ]


# ------------------------------------------------------------------ ladder
def extract_with_ladder(fetch, url: str, *, max_pages: int = 3,
                        already: set[str] | None = None,
                        render=None) -> dict:
    """Try every strategy against `url`, in order. Returns a report:
    {rows, strategy, pages, notes, embeds} - never raises on failure.

    `fetch(url) -> str` is injectable (tests); the real one wraps the
    fetch ladder. `render(url) -> str` (headless Chromium) is injectable
    too - defaults to scrapling/selenium's render_dynamic. `already`
    collects names seen to keep rows unique.
    """
    html = fetch(url)
    embeds = detect_embeds(html)
    notes: list[str] = []
    pages = [url]
    seen: set[str] = set(already or set())
    rows: list[dict] = []

    def collect(new_rows: list[dict]) -> None:
        for row in new_rows:
            key = row.get("name", "").lower()
            if key and key not in seen:
                seen.add(key)
                rows.append(row)

    def extract_all(page_html: str) -> list[dict]:
        """static -> cards -> json; first pass with ANY rows wins (a
        pagination page can hold fewer than 3 rows and still be real)."""
        for found in (exhibitors_from_static(page_html, url),
                      exhibitors_from_cards(page_html, url),
                      exhibitors_from_json(page_html)):
            if found:
                return found
        return []

    # Strategy ladder. Static first (deliberately narrow), then cards,
    # then embedded JSON. SPA pages (Next/Nuxt) skip cards+params
    # entirely: their class tokens are UI chrome and their lists load
    # via XHR - only the browser render can get the rows (found live on
    # biofach.de: Algolia POST behind /find-exhibitors).
    spa = bool(embeds["next_data"] or embeds["nuxt"])
    if spa:
        notes.append("SPA payload detected - skipping card/param heuristics, "
                     "going to browser render + XHR capture")

    static = exhibitors_from_static(html, url)
    if len(static) >= 3:
        collect(static)
        return {"rows": rows, "strategy": "static", "pages": pages,
                "notes": notes, "embeds": embeds}

    if not spa:
        cards = exhibitors_from_cards(html, url)
        if len(cards) >= 3:
            collect(cards)
            return {"rows": rows, "strategy": "cards", "pages": pages,
                    "notes": notes, "embeds": embeds}

    json_rows = exhibitors_from_json(html)
    if len(json_rows) >= 3:
        collect(json_rows)
        return {"rows": rows, "strategy": "json", "pages": pages,
                "notes": notes, "embeds": embeds}

    # params: the list is a component fed by URL parameters. STAGED:
    # rows are only claimed when a param page actually ADDS new rows
    # (2 stray rows on the landing page are usually junk, not page 1 of
    # a paginated list) - otherwise fall through to the browser render
    # with nothing committed.
    if not spa and (len(static) >= 1 or embeds["iframes"]
                    or re.search(r"pagination|\bpage=\d|next\s*page",
                                 html, re.I)):
        staged = list(static)
        staged_seen = {r["name"].lower() for r in staged}
        added_new = False
        for page in range(2, max_pages + 1):
            page_rows: list[dict] = []
            for variant in param_variants(url, page):
                try:
                    vhtml = fetch(variant)
                except Exception:  # noqa: BLE001 - a bad variant is not fatal
                    continue
                if not vhtml or vhtml == html:
                    continue
                if variant not in pages:
                    pages.append(variant)
                page_rows = extract_all(vhtml)
                if page_rows:
                    break
            fresh = [r for r in page_rows
                     if r.get("name", "").lower() not in staged_seen]
            if not fresh:
                break   # no new rows -> the list ended (or never existed)
            added_new = True
            staged_seen.update(r["name"].lower() for r in fresh)
            staged.extend(fresh)
        if added_new:
            collect(staged)
            return {"rows": rows, "strategy": "params", "pages": pages,
                    "notes": notes, "embeds": embeds}
        notes.append("param pages added no new rows - not a params list")

    # dynamic: headless Chromium (scrapling -> selenium) with the generic
    # scenario; captured XHR payloads (Algolia/API) are mined FIRST - the
    # rendered DOM of widget lists is mostly UI chrome
    if render is None:
        from buyerintel.exhibitors.dynamic import render_dynamic_capture
        render = render_dynamic_capture
    try:
        out = render(url)
        if isinstance(out, tuple):
            final_html, payloads = out
        else:
            final_html, payloads = out, []
        payload_rows: list[dict] = []
        for payload in payloads:
            payload_rows.extend(exhibitors_from_payload_json(payload))
        if len(payload_rows) >= 3:
            collect(payload_rows)
            return {"rows": rows, "strategy": "dynamic-payload",
                    "pages": pages, "notes": notes, "embeds": embeds}
        collect(extract_all(final_html))
        if rows:
            return {"rows": rows, "strategy": "dynamic", "pages": pages,
                    "notes": notes, "embeds": embeds}
        notes.append("dynamic render produced no exhibitor rows")
    except DynamicUnavailable as exc:
        notes.append(f"dynamic rendering unavailable: {exc}")
    return {"rows": rows, "strategy": "dynamic", "pages": pages,
            "notes": notes, "embeds": embeds}


# ------------------------------------------------------------------ ingest
def _derive_expo_name(html: str, url: str) -> tuple[str, int | None]:
    """(expo name, year) from the page title / URL.

    Splits the title on separators and takes the SHORTEST segment (found
    live 2026-11-16 on biofach.de: 'The world's leading trade fair for
    organic food: Biofach Trade Fair' -> the tagline outlived the name),
    then strips trailing fair-words and the year.
    """
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
    if m:
        title = _clean_text(re.sub(r"<[^>]+>", " ", m.group(1)))
    segments = [s.strip(" -–—|") for s in re.split(
        r"(?:\s+(?:[|]|\-{1,2}|\u2013|\u2014|::|\u00bb)|\s*:\s+)", title)]
    segments = [s for s in segments if len(s) >= 3]
    if segments:
        name = min(segments, key=len)
    else:
        name = title
    year = None
    ym = re.search(r"\b(20\d{2})\b", f"{title} {url}")
    if ym:
        year = int(ym.group(1))
    name = re.sub(r"\b20\d{2}\b", "", name).strip(" -–|")
    name = re.sub(r"\s+(?:trade\s+fairs?|messe|expo|exhibition)$",
                  "", name, flags=re.I).strip(" -–|")
    if not name or len(name) < 3:
        try:
            name = (urlparse(url).hostname or "").removeprefix("www.")
        except ValueError:
            name = "Unknown Expo"
    return name[:120], year


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:40] \
        or "expo"


def _write_ingest_files(rows: list[dict], expo_name: str, stem: str) \
        -> tuple[Path, Path]:
    """Temp CSV + generated mapping YAML (existing importer schema)."""
    import csv
    import yaml

    tmp = Path(tempfile.mkdtemp(prefix=f"calendula_mine_{stem}_"))
    csv_path = tmp / "exhibitors.csv"
    mapping_path = tmp / "mapping.yaml"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(["Name", "Website", "Country", "City"])
        for row in rows:
            writer.writerow([row.get("name", ""), row.get("website", ""),
                             row.get("country", ""), row.get("city", "")])
    mapping_path.write_text(yaml.safe_dump({
        "expo": expo_name,
        "source": f"auto_{stem}",
        "sheet": None,
        "columns": {
            "name": ["Name"],
            "domain": ["Website"],
            "country": ["Country"],
            "city": ["City"],
        },
    }, allow_unicode=True), encoding="utf-8")
    return csv_path, mapping_path


def auto_mine_expo(session, expo_url: str, *, expo_name: str | None = None,
                   year: int | None = None, max_pages: int = 3,
                   fetch=None, render=None) -> dict:
    """Mine one expo's exhibitor list end to end and INGEST it.

    Detection is automatic: list page, embedded component vs original
    page, static vs cards vs JSON vs parameters vs headless Chromium
    (scrapling -> selenium). Ingestion reuses import_workbook
    (idempotent, expo appearances included), so re-running mines
    nothing new.
    """
    from buyerintel.enrich.fetch import FetchLadder

    real_fetch = fetch

    def ladder_fetch(url: str) -> str:
        if real_fetch is not None:
            return real_fetch(url)
        return FetchLadder(session).fetch(url).html

    report: dict = {"url": expo_url, "rows_found": 0, "imported": None,
                    "expo": expo_name, "strategy": None, "notes": []}
    html = ladder_fetch(expo_url)
    name_guess, year_guess = _derive_expo_name(html, expo_url)
    expo_name = expo_name or name_guess
    # default the edition year to "now" so appearances always register
    # (import_workbook only creates an edition when a year is given)
    year = year or year_guess or datetime.now().year
    report["expo"] = expo_name

    # step 1: find the actual list page - TRY THE TOP CANDIDATES FIRST;
    # the landing page is the fallback ONLY when no candidates were
    # found at all. When candidates exist but all yield junk, mining
    # the landing page anyway just imports its partner/press links
    # (found live 2026-11-16) - report failure instead.
    candidates = find_list_pages(html, expo_url)
    result = None
    list_url, list_kind = expo_url, "original page"
    for cand in candidates[:3]:
        if cand["url"] == expo_url:
            continue
        try:
            cand_result = extract_with_ladder(
                ladder_fetch, cand["url"], max_pages=max_pages,
                render=render)
        except Exception as exc:  # noqa: BLE001 - candidate down is not fatal
            report["notes"].append(f"candidate {cand['url']} failed: {exc}")
            continue
        if len(cand_result["rows"]) >= 3:
            result = cand_result
            list_url, list_kind = cand["url"], cand["kind"]
            break
        report["notes"].append(
            f"candidate {cand['url']} yielded "
            f"{len(cand_result['rows'])} row(s) - skipped")
    if result is None and not candidates:
        result = extract_with_ladder(ladder_fetch, expo_url,
                                     max_pages=max_pages, render=render)
        list_url, list_kind = expo_url, "original page"
    if result is None:
        # candidates existed but none yielded exhibitors - do NOT mine the
        # landing page (its partner/press links are not the list)
        report["list_page"] = None
        report["list_kind"] = None
        report["status"] = "no exhibitors extracted"
        report["notes"].append("all candidate list pages yielded no "
                               "exhibitors - landing page not mined")
        return report
    report["list_page"] = list_url
    report["list_kind"] = list_kind

    report.update({
        "strategy": result["strategy"],
        "pages": result["pages"],
        "embeds": result["embeds"],
        "notes": report["notes"] + result["notes"],
        "rows_found": len(result["rows"]),
    })
    if not result["rows"]:
        report["status"] = "no exhibitors extracted"
        return report

    stem = _slug(expo_name)
    csv_path, mapping_path = _write_ingest_files(result["rows"], expo_name, stem)
    stats = import_workbook(session, csv_path, mapping_path,
                            expo_name=expo_name, expo_year=year)
    appearances = create_appearances(session)
    report["imported"] = stats.__dict__
    report["appearances_created"] = appearances
    report["status"] = "ok"
    return report
