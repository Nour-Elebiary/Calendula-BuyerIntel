"""Auto-mining of exhibitor lists (owner 2026-11-16): the system must
detect on its own the right exhibitor list page, whether the list is an
embedded component or the original page, and work out the extraction
strategy (static / JSON payload / URL parameters / headless Chromium).

All tests use inline fixture HTML and an injected fetch - never the
network, never a real browser.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from buyerintel.db.models import Company, Expo, ExpoEdition, ExpoAppearance, \
    SourceRecord
from buyerintel.exhibitors.auto import (
    auto_mine_expo,
    detect_embeds,
    exhibitors_from_cards,
    exhibitors_from_json,
    exhibitors_from_payload_json,
    exhibitors_from_static,
    extract_with_ladder,
    find_list_pages,
    param_variants,
    _derive_expo_name,
)
from buyerintel.exhibitors.dynamic import DynamicUnavailable

# ------------------------------------------------------------------ fixtures
LANDING = """
<html><head><title>BioFach 2026 - World's Leading Trade Fair for Organic Food</title></head>
<body>
  <nav><a href="/program">Program</a><a href="/tickets">Tickets</a></nav>
  <a href="/exhibitors-companies-2026">Exhibitors & Companies</a>
  <a href="/news">News</a>
  <a href="/exhibit/become-exhibitor">Become exhibitor at BIOFACH</a>
  <a href="/exhibit/exhibitor-chatbot">Chatbot for exhibitors - ask now!</a>
  <iframe src="https://widget.expo.dev/list?event=biofach"></iframe>
</body></html>
"""

STATIC_LIST = """
<html><title>Exhibitors - BioFach</title><body>
<div class="list">
  <a href="/exhibitor/kräuter-klang-gmbh">Kräuter Klang GmbH</a>
  <a href="/exhibitor/tea-traders">Tea Traders Ltd</a>
  <a href="/exhibitor/spice-house">Spice House S.A.</a>
  <a href="https://www.chamomileco.de">Chamomile & Co</a>
  <a href="/exhibitor/all">View All Exhibitors</a>
  <a href="/register">Register</a>
</div></body></html>
"""

JSON_LIST = """
<html><body><script id="__NEXT_DATA__" type="application/json">
{"props": {"exhibitors": [
  {"name": "Kräuter Klang GmbH", "country": "DEU", "city": "Hamburg",
   "website": "https://kraeuter-klang.de"},
  {"name": "Tea Traders Ltd", "country": "GBR"},
  {"name": "Spice House S.A.", "country": "FRA"},
  {"name": "Chamomile & Co", "country": "EGY"}
]}}</script></body></html>
"""

EMPTY_SPA = """
<html><head><title>Expo Exhibitors</title></head>
<body><div id="root"></div>
<script src="/js/app.js"></script>
<script>window.__NUXT__=(function(a,b){return {x:[a,b]}}('a','b'));</script>
</body></html>
"""

# card list WITHOUT profile anchors - only the scrapling class-token
# grouping path can read it (nested markup defeats the regex pass)
CARDS_LIST = """
<html><body>
<div class="exhibitor-card"><h3>Kräuter Klang GmbH</h3>
  <a href="https://kraeuter-klang.de">website</a></div>
<div class="exhibitor-card"><h3>Tea Traders Ltd</h3>
  <a href="https://teatraders.co.uk">website</a></div>
<div class="exhibitor-card"><h3>Spice House S.A.</h3>
  <a href="https://spicehouse.fr">website</a></div>
<div class="other"><h3>Ignore me</h3></div>
</body></html>
"""

# card list WITH nested profile anchors - the anchors path (xpath string(.))
CARDS_ANCHORS = """
<html><body>
<div class="card">
  <a href="/exhibitor/kräuter-klang-gmbh"><span>Kräuter Klang</span>
    <small>Hall 1 / Stand 12</small></a></div>
<div class="card">
  <a href="/exhibitor/tea-traders"><span>Tea Traders Ltd</span>
    <small>Hall 2 / Stand 4</small></a></div>
<div class="card">
  <a href="/exhibitor/spice-house"><span>Spice House S.A.</span>
    <small>Hall 2 / Stand 7</small></a></div>
</body></html>
"""

# Algolia-style XHR payload: real hits have name + structured signal;
# CMS junk (navigation, style vars) has no company signal
ALGOLIA_PAYLOAD = """
{"results": [{"hits": [
  {"companyName": "\\"Dary Natury\\" Sp. z o.o.", "slogan": null,
   "country": "Poland", "booth": [{"boothNumber": "5-443", "boothHall": "5"}]},
  {"companyName": "A. Dohrn A. Timm GmbH & Co. KG", "slogan": null,
   "country": "Germany", "booth": [{"boothNumber": "1-221", "boothHall": "1"}]},
  {"companyName": "1st Vegan GmbH", "country": "Germany",
   "booth": [{"boothNumber": "4-550", "boothHall": "4"}]}
], "nbHits": 3}]}
"""

JUNK_PAYLOAD = """
{"site": {"name": "bio", "language": "en"},
 "nav": [{"id": 1, "name": "Footer Navigation", "url": "/en/footer"},
         {"id": 2, "name": "Editor Page", "url": "/editor"}],
 "style": [{"name": "colorActionHighlightHover", "value": "#336699"}]}
"""


PAGE1 = """
<html><title>Exhibitors</title><body>
<div class="list">
  <a href="/exhibitor/kräuter-klang-gmbh">Kräuter Klang GmbH</a>
  <a href="/exhibitor/tea-traders">Tea Traders Ltd</a>
</div>
<a class="next" href="/exhibitors?page=2">next page</a>
</body></html>
"""

PAGE2 = """
<html><title>Exhibitors 2</title><body>
<div class="list">
  <a href="/exhibitor/spice-house">Spice House S.A.</a>
  <a href="/exhibitor/chamomile-co">Chamomile & Co</a>
</div>
</body></html>
"""


class FakeFetch:
    """url -> html table; unknown urls raise like a 404."""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.fetched: list[str] = []

    def __call__(self, url: str) -> str:
        self.fetched.append(url)
        if url not in self.pages:
            raise LookupError(url)
        return self.pages[url]


# ------------------------------------------------------------------ detect
def test_find_list_pages_detects_and_ranks():
    hits = find_list_pages(LANDING, "https://biofach.example")
    assert hits, "no list-page candidates found"
    assert hits[0]["url"] == "https://biofach.example/exhibitors-companies-2026"
    assert hits[0]["kind"] == "link"
    # the iframe embed is detected as an embedded component
    kinds = {h["kind"] for h in hits}
    assert "embed" in kinds
    # junk pages are not candidates
    assert not any("/tickets" in h["url"] for h in hits)


def test_detect_embeds_payload_markers():
    embeds = detect_embeds(LANDING)
    assert embeds["iframes"] == ["https://widget.expo.dev/list?event=biofach"]
    embeds = detect_embeds(JSON_LIST)
    assert embeds["next_data"] is True
    embeds = detect_embeds(EMPTY_SPA)
    assert embeds["nuxt"] is True


# ------------------------------------------------------------------ extract
def test_exhibitors_from_static():
    rows = exhibitors_from_static(STATIC_LIST, "https://biofach.example")
    names = [r["name"] for r in rows]
    # deliberately NARROW: profile links only - the external Chamomile
    # anchor must NOT become a row (partner/press-link protection)
    assert names == ["Kräuter Klang GmbH", "Tea Traders Ltd",
                     "Spice House S.A."]


def test_exhibitors_from_cards_token_grouping():
    # no profile anchors -> scrapling class-token grouping path
    rows = exhibitors_from_cards(CARDS_LIST, "https://expo.example/list")
    names = [r["name"] for r in rows]
    assert "Kräuter Klang GmbH" in names
    assert "Tea Traders Ltd" in names
    assert "Spice House S.A." in names
    assert not any("ignore" in n.lower() for n in names)
    # external website anchors inside the card become the domain
    kraut = next(r for r in rows if "kräuter" in r["name"].lower())
    assert kraut["website"] == "kraeuter-klang.de"


def test_exhibitors_from_cards_nested_anchors():
    # nested markup anchors -> xpath string(.) text extraction
    rows = exhibitors_from_cards(CARDS_ANCHORS, "https://expo.example/list")
    names = [r["name"] for r in rows]
    assert names == ["Kräuter Klang Hall 1 / Stand 12",
                     "Tea Traders Ltd Hall 2 / Stand 4",
                     "Spice House S.A. Hall 2 / Stand 7"] \
        or names == ["Kräuter Klang", "Tea Traders Ltd", "Spice House S.A."]
    assert len(names) == 3


def test_exhibitors_from_json_payload():
    rows = exhibitors_from_json(JSON_LIST)
    assert [r["name"] for r in rows] == ["Kräuter Klang GmbH", "Tea Traders Ltd",
                                         "Spice House S.A.", "Chamomile & Co"]
    assert rows[0]["country"] == "DEU"
    assert rows[0]["city"] == "Hamburg"
    assert rows[0]["website"] == "https://kraeuter-klang.de"


def test_param_variants_cover_common_widget_params():
    variants = param_variants("https://expo.example/exhibitors", page=2)
    assert variants[0] == "https://expo.example/exhibitors?page=2"
    assert "exhibitors=1" in variants[1] and "page=2" in variants[1]
    # existing query strings get '&' separators, not '?'
    variants = param_variants("https://expo.dev/list?event=biofach", page=3)
    assert variants[0].endswith("&page=3")


def test_derive_expo_name_and_year():
    name, year = _derive_expo_name(LANDING, "https://biofach.example/2026")
    assert year == 2026
    assert "biofach" in name.lower()


# ------------------------------------------------------------------ ladder
def test_ladder_static_strategy():
    fetch = FakeFetch({"https://biofach.example/exhibitors": STATIC_LIST})
    out = extract_with_ladder(fetch, "https://biofach.example/exhibitors")
    assert out["strategy"] == "static"
    assert len(out["rows"]) == 3
    assert out["pages"] == ["https://biofach.example/exhibitors"]


def test_ladder_json_strategy():
    fetch = FakeFetch({"https://expo.example/list": JSON_LIST})
    out = extract_with_ladder(fetch, "https://expo.example/list")
    assert out["strategy"] == "json"
    assert len(out["rows"]) == 4


def test_ladder_payload_outranks_static_anchors():
    # biofach live case: the static HTML carries partner/press links
    # (external anchors) while the REAL exhibitor list lives in __NEXT_DATA__
    polluted = ('<html><body>'
                '<a href="https://press1.de">press partner one</a>'
                '<a href="https://press2.at">press partner two</a>'
                '<a href="https://press3.ch">press partner three</a>'
                + JSON_LIST + "</body></html>")
    fetch = FakeFetch({"https://expo.example/list": polluted})
    out = extract_with_ladder(fetch, "https://expo.example/list")
    assert out["strategy"] == "json"
    names = [r["name"] for r in out["rows"]]
    assert "Kräuter Klang GmbH" in names
    assert not any("press partner" in n for n in names)


def test_ladder_cards_strategy_token_grouping():
    # static regex finds 0 (no profile anchors) -> cards wins before json
    fetch = FakeFetch({"https://expo.example/list": CARDS_LIST})
    out = extract_with_ladder(fetch, "https://expo.example/list")
    assert out["strategy"] == "cards"
    assert len(out["rows"]) == 3


def test_ladder_static_beats_cards_when_both_work():
    # profile anchors present -> static regex wins, cards not consulted
    fetch = FakeFetch({"https://expo.example/list": CARDS_ANCHORS})
    out = extract_with_ladder(fetch, "https://expo.example/list")
    assert out["strategy"] == "static"
    assert len(out["rows"]) == 3


def test_ladder_dynamic_degrades_without_browser():
    # no fetch fallback for the SPA and an injectable render that fails:
    # the ladder must degrade with a note, never raise
    def broken_render(url: str) -> str:
        raise DynamicUnavailable("no browser in test")

    fetch = FakeFetch({"https://expo.example/app": EMPTY_SPA})
    out = extract_with_ladder(fetch, "https://expo.example/app",
                              render=broken_render)
    assert out["strategy"] == "dynamic"
    assert out["rows"] == []
    assert any("unavailable" in n.lower() for n in out["notes"])


def test_ladder_dynamic_uses_injected_render():
    # a successful injected render feeds the SAME extractors
    def good_render(url: str) -> str:
        return STATIC_LIST

    fetch = FakeFetch({"https://expo.example/app": EMPTY_SPA})
    out = extract_with_ladder(fetch, "https://expo.example/app",
                              render=good_render)
    assert out["strategy"] == "dynamic"
    assert len(out["rows"]) == 3


def test_ladder_params_strategy():
    fetch = FakeFetch({
        "https://expo.example/exhibitors": PAGE1,
        "https://expo.example/exhibitors?page=2": PAGE2,
        "https://expo.example/exhibitors?page=2&exhibitors=1": PAGE2,
    })
    out = extract_with_ladder(fetch, "https://expo.example/exhibitors")
    assert out["strategy"] == "params"
    names = [r["name"] for r in out["rows"]]
    assert names == ["Kräuter Klang GmbH", "Tea Traders Ltd",
                     "Spice House S.A.", "Chamomile & Co"]
    assert any("page=2" in p for p in out["pages"])


def test_ladder_params_falls_through_when_no_new_rows():
    # page 1 has stray rows but page=2 returns the SAME content: not a
    # paginated list - nothing committed, ladder continues to dynamic
    fetch = FakeFetch({"https://expo.example/exhibitors": PAGE1})

    def render(url: str) -> str:
        raise DynamicUnavailable("no browser in test")

    out = extract_with_ladder(fetch, "https://expo.example/exhibitors",
                              render=render)
    assert out["strategy"] == "dynamic"
    assert out["rows"] == []


def test_ladder_spa_skips_cards_and_params():
    # biofach live case: Next.js SPA - card/param heuristics on the
    # static HTML must NOT claim the page; the browser render decides
    fetch = FakeFetch({"https://expo.example/find": EMPTY_SPA})

    def render(url: str) -> str:
        return CARDS_LIST   # the rendered DOM would have real cards

    out = extract_with_ladder(fetch, "https://expo.example/find",
                              render=render)
    assert out["strategy"] == "dynamic"
    assert not any("added no new rows" in n for n in out["notes"])
    assert len(out["rows"]) == 3


def test_exhibitors_from_payload_json_algolia_hits():
    rows = exhibitors_from_payload_json(ALGOLIA_PAYLOAD)
    assert [r["name"] for r in rows] == ["Dary Natury Sp. z o.o.",
                                         "A. Dohrn A. Timm GmbH & Co. KG",
                                         "1st Vegan GmbH"]
    assert rows[0]["country"] == "Poland"


def test_exhibitors_from_payload_json_rejects_cms_junk():
    rows = exhibitors_from_payload_json(JUNK_PAYLOAD)
    assert rows == []


def test_ladder_dynamic_payload_outranks_dom_chrome():
    # dynamic render returns (html, payloads) - payload rows win over
    # the UI-chrome DOM (render returns a tuple when capture is active)
    chrome_dom = ("<html><body>"
                  "<a href='https://shop.nuernbergmesse.de'>Order services</a>"
                  "</body></html>")

    def capture_render(url: str) -> tuple[str, list[str]]:
        return chrome_dom, [ALGOLIA_PAYLOAD, JUNK_PAYLOAD]

    fetch = FakeFetch({"https://expo.example/find": EMPTY_SPA})
    out = extract_with_ladder(fetch, "https://expo.example/find",
                              render=capture_render)
    assert out["strategy"] == "dynamic-payload"
    names = [r["name"] for r in out["rows"]]
    assert "Dary Natury Sp. z o.o." in names
    assert not any("order services" in n.lower() for n in names)


def test_selenium_scenario_raises_clear_error_when_absent():
    pytest.importorskip("buyerintel.exhibitors.dynamic")
    import buyerintel.exhibitors.dynamic as dyn

    if dyn.selenium_available():
        pytest.skip("selenium is installed here")
    with pytest.raises(DynamicUnavailable):
        dyn.browser_scenario("https://example.com")


# ------------------------------------------------------------------ ingest
def test_auto_mine_end_to_end_static(session):
    fetch = FakeFetch({
        "https://biofach.example/": LANDING,
        "https://biofach.example/exhibitors-companies-2026": STATIC_LIST,
    })
    report = auto_mine_expo(session, "https://biofach.example/", fetch=fetch)
    assert report["status"] == "ok"
    assert report["strategy"] == "static"
    assert report["list_page"] == \
        "https://biofach.example/exhibitors-companies-2026"
    assert report["rows_found"] == 3
    assert report["imported"]["companies_created"] == 3
    names = set(session.scalars(select(Company.canonical_name)).all())
    # import_workbook canonicalizes names (legal form stripped, lowercase)
    assert {"kräuter klang", "tea traders", "spice house"} <= names
    assert session.query(Expo).count() == 1
    assert session.query(ExpoAppearance).count() == 3
    assert session.query(ExpoEdition).count() == 1


def test_auto_mine_idempotent_rerun(session):
    fetch = FakeFetch({
        "https://biofach.example/": LANDING,
        "https://biofach.example/exhibitors-companies-2026": STATIC_LIST,
    })
    first = auto_mine_expo(session, "https://biofach.example/", fetch=fetch)
    session.commit()
    second = auto_mine_expo(session, "https://biofach.example/", fetch=fetch)
    assert first["imported"]["companies_created"] == 3
    assert second["rows_found"] == 3
    assert second["imported"]["rows_duplicate"] == 3
    assert session.query(Company).count() == 3


def test_auto_mine_no_rows_never_raises(session):
    fetch = FakeFetch({"https://spa.example/": EMPTY_SPA})
    report = auto_mine_expo(session, "https://spa.example/", fetch=fetch)
    assert report["status"] != "ok"
    assert report["rows_found"] == 0
    assert session.query(Company).count() == 0


def test_auto_mine_no_landing_fallback_when_candidates_yield_junk(session):
    # candidates exist but all yield <3 rows: the landing page must NOT
    # be mined (its partner/press links are not the list) - report failure
    fetch = FakeFetch({"https://biofach.example/": LANDING})

    def render(url: str) -> str:
        raise DynamicUnavailable("no browser in test")

    report = auto_mine_expo(session, "https://biofach.example/", fetch=fetch,
                            render=render)
    assert report["status"] == "no exhibitors extracted"
    assert report["rows_found"] == 0
    assert report["imported"] is None
    assert session.query(Company).count() == 0
