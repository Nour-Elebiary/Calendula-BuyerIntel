"""Non-login LinkedIn public-profile scraper (ADD-012, spec section 12.8).

The DEFAULT path for person enrichment: fetches PUBLIC profile pages with
no login, no cookies, no account - per-domain pacing, run for all tiers.

Backend: `builtin` parses public profile HTML with a pure parser (testable
against saved fixtures, per the spec's HTML-fixture policy; no extra parser
dependency - the project parses with stdlib regex throughout). The external
ToddNieb scraper (ADD-012) can be plugged in as a separate-process backend
later. Missing fields are normal; the parser never invents values.

Config (config/enrich.yaml `linkedin.public_scraper`):
  enabled: true    # the non-login path is ON by default per spec 12.8
  backend: builtin
  pacing_s: 3      # per-domain pacing
"""
from __future__ import annotations

import re
import time

import httpx

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

TAG_RE = re.compile(r"<[^>]+>")
H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)
HEADLINE_RE = re.compile(r'<div[^>]*class="[^"]*text-body-medium[^"]*"[^>]*>\s*<span[^>]*>([^<]{2,200})',
                         re.I)
LOCATION_RE = re.compile(r'<span[^>]*class="[^"]*text-body-small[^"]*"[^>]*>\s*<span[^>]*>([^<]{2,120})',
                         re.I)
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def _text(fragment: str) -> str:
    """Strip tags + entities-ish whitespace from one HTML fragment."""
    import html as html_mod

    return re.sub(r"\s+", " ", html_mod.unescape(TAG_RE.sub(" ", fragment))).strip()


def parse_profile_html(html: str) -> dict | None:
    """Parse a PUBLIC profile page into a person dict (no network, no login).

    Returns {full_name, headline?, location?, email?, title?, source}
    or None when the page carries no profile (auth wall, empty, 404 body) -
    never invents a value.
    """
    if not html or not html.strip():
        return None
    lowered = html.lower()
    if "authwall" in lowered or "/login" in lowered:
        title_m = TITLE_TAG_RE.search(html)
        page_title = _text(title_m.group(1)).lower() if title_m else ""
        if "authwall" in lowered or "sign in" in page_title:
            return None

    out: dict = {"source": "linkedin_public"}
    h1 = H1_RE.search(html)
    if not h1:
        return None
    out["full_name"] = _text(h1.group(1))
    if not out["full_name"]:
        return None

    headline = HEADLINE_RE.search(html)
    if headline:
        out["headline"] = _text(headline.group(1))
    location = LOCATION_RE.search(html)
    if location:
        out["location"] = _text(location.group(1))
    email = EMAIL_RE.search(html)
    if email:
        out["email"] = email.group(0).lower()

    headline_text = out.get("headline") or ""
    if " at " in headline_text.lower():
        # 'Head of Purchasing at Acme GmbH' -> title = 'Head of Purchasing'
        out["title"] = re.split(r" at ", headline_text, flags=re.I)[0].strip()
    return out


def fetch_profile(url: str, *, config: dict | None = None) -> dict | None:
    """Fetch one PUBLIC profile page with no account; parse what is visible.

    Degrades gracefully: auth walls, network errors and non-200s return
    None - never a crash, never a fabricated field. Sends NO cookies and
    no credentials of any kind (spec 12.8 'no account involved').
    """
    cfg = (config or {}).get("linkedin", {}).get("public_scraper", {})
    if not cfg.get("enabled", True):
        return None
    pacing = float(cfg.get("pacing_s", 3.0))
    time.sleep(pacing)  # per-domain pacing, single-user polite rate
    try:
        response = httpx.get(url, headers={"User-Agent": USER_AGENT},
                             timeout=20.0, follow_redirects=False)
    except Exception:  # noqa: BLE001 - network problems are not crashes
        return None
    if response.status_code != 200:
        return None
    return parse_profile_html(response.text)
