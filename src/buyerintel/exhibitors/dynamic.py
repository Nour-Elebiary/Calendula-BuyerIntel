"""Generic headless-browser scenario for lists that only render in a real
browser (dynamic strategy, auto-mining ladder step 4).

PRIMARY ENGINE: Scrapling (a project dependency) - StealthyFetcher runs
Playwright's Chromium with stealth defaults and a `page_action` hook, which
is where the generic load/scroll/load-more scenario lives. The community
built Scrapling exactly for this; its Chromium needs a one-time
`uv run python -m playwright install chromium`.

FALLBACK ENGINE: Selenium (optional dep) with Chrome/Edge for setups
without scrapling. The ladder degrades with a clear message when neither
is available - never crashes the discovery flow. The scenario is
deliberately GENERIC - load, wait, scroll (lazy lists), click load-more
buttons, hand the final DOM back to the same static/JSON extractors.
Per-site scripts are not maintained.
"""
from __future__ import annotations

import re
import time


class DynamicUnavailable(Exception):
    """No usable headless browser (scrapling/selenium) on this machine."""


LOAD_MORE_RE = re.compile(
    r"(?:load|show|view)\s+more|mehr\s+anzeigen|afficher\s+plus", re.I)


def scrapling_available() -> bool:
    """True when scrapling's Chromium stack can be used."""
    try:
        from scrapling.fetchers import StealthyFetcher  # noqa: F401

        return True
    except Exception:  # noqa: BLE001 - missing curl_cffi/playwright etc.
        return False


def _scrapling_scenario(url: str, *, scrolls: int = 4,
                        settle_seconds: float = 1.0,
                        captured: list[str] | None = None) -> str:
    """Scrapling StealthyFetcher (Playwright Chromium): scroll the page a
    few times and click visible load-more buttons, then return the DOM.

    When `captured` is a list, JSON responses the page fetches while
    rendering (Algolia/GraphQL/API exhibitor payloads) are collected into
    it - embedded components often serve their rows ONLY via XHR.
    """
    from scrapling.fetchers import StealthyFetcher

    def action(page) -> None:
        if captured is not None:
            def on_response(resp) -> None:
                try:
                    ctype = (resp.headers or {}).get("content-type", "")
                except Exception:  # noqa: BLE001
                    ctype = ""
                if "json" not in ctype.lower():
                    return
                if resp.request.method.upper() != "POST" \
                        and "api" not in resp.url.lower() \
                        and "graphql" not in resp.url.lower() \
                        and "algolia" not in resp.url.lower():
                    return
                try:
                    body = resp.text()
                except Exception:  # noqa: BLE001
                    return
                if body and len(body) < 2_000_000:
                    captured.append(body)

            page.on("response", on_response)
            # scrapling's own goto already finished BEFORE page_action runs,
            # so the page's initial XHRs (Algolia/API exhibitor payloads)
            # fired while nobody listened - reload to catch the fresh load
            try:
                page.reload(wait_until="networkidle")
            except Exception:  # noqa: BLE001 - reload is best-effort
                pass
        for _ in range(max(1, scrolls)):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(int(settle_seconds * 1000))
            try:
                buttons = page.locator("xpath=//button | //a[@role='button']")
                for i in range(buttons.count()):
                    btn = buttons.nth(i)
                    text = (btn.text_content() or "").strip()
                    if text and LOAD_MORE_RE.search(text) and btn.is_visible():
                        btn.click()
                        page.wait_for_timeout(int(settle_seconds * 1000))
                        break
            except Exception:  # noqa: BLE001 - locator quirks are not fatal
                continue

    try:
        response = StealthyFetcher.fetch(
            url, headless=True, network_idle=True,
            page_action=action, timeout=90000)
        html = response.html_content
        if not html:
            raise DynamicUnavailable("scrapling scenario returned empty DOM")
        return html
    except DynamicUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - any playwright failure degrades
        raise DynamicUnavailable(f"scrapling scenario failed: {exc}") from exc


def render_dynamic(url: str, *, scrolls: int = 4,
                   settle_seconds: float = 1.5) -> str:
    """Render `url` in headless Chromium (scrapling first, selenium second)
    and return the final page source."""
    final_html, _payloads = render_dynamic_capture(
        url, scrolls=scrolls, settle_seconds=settle_seconds,
        selenium_only=False)
    return final_html


def render_dynamic_capture(url: str, *, scrolls: int = 4,
                           settle_seconds: float = 1.5,
                           selenium_only: bool = False) -> tuple[str, list[str]]:
    """Render `url` in headless Chromium and return (final DOM, captured
    JSON payloads). Capturing needs scrapling - the selenium fallback
    returns no payloads (its DOM extraction still applies)."""
    if not selenium_only and scrapling_available():
        payloads: list[str] = []
        try:
            html = _scrapling_scenario(url, scrolls=scrolls,
                                       settle_seconds=settle_seconds,
                                       captured=payloads)
            return html, payloads
        except DynamicUnavailable:
            pass   # fall through to selenium
    if selenium_available():
        return browser_scenario(url, scrolls=scrolls,
                                settle_seconds=settle_seconds), []
    raise DynamicUnavailable(
        "no browser engine available; dynamic rendering needs scrapling's "
        "Chromium (uv run python -m playwright install chromium) or selenium")


def selenium_available() -> bool:
    """True when the selenium package can be imported."""
    try:
        import selenium  # noqa: F401

        return True
    except ImportError:
        return False


def _make_driver():
    """Headless Chromium via Chrome, falling back to Edge (always present on
    Windows). Selenium Manager (selenium >= 4.6) fetches the driver itself."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions

    for driver_factory in (_chrome_driver, _edge_driver):
        try:
            return driver_factory()
        except Exception:  # noqa: BLE001 - try the next browser
            continue
    raise DynamicUnavailable(
        "no usable Chromium browser (Chrome/Edge) for selenium")


def _chrome_driver():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--window-size=1400,1000")
    return webdriver.Chrome(options=opts)


def _edge_driver():
    from selenium import webdriver
    from selenium.webdriver.edge.options import Options

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1400,1000")
    return webdriver.Edge(options=opts)


def browser_scenario(url: str, *, scrolls: int = 4,
                     settle_seconds: float = 1.5) -> str:
    """Render `url` in headless Chromium and return the final page source.

    Generic scenario: wait for the document, scroll to the bottom a few
    times (lazy-loaded lists), click visible load-more buttons, then return
    the DOM as HTML for the static/JSON extractors.
    """
    if not selenium_available():
        raise DynamicUnavailable(
            "selenium is not installed - run: uv add selenium")
    driver = None
    try:
        driver = _make_driver()
        driver.get(url)
        time.sleep(settle_seconds)
        for _ in range(max(1, scrolls)):
            driver.execute_script(
                "window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(settle_seconds)
            clicked = False
            for el in driver.find_elements("xpath",
                                           "//button|//a[@role='button']"):
                try:
                    text = (el.text or "").strip()
                    if text and LOAD_MORE_RE.search(text):
                        el.click()
                        clicked = True
                        time.sleep(settle_seconds)
                        break
                except Exception:  # noqa: BLE001 - stale/stale element
                    continue
            if not clicked:
                break
        return driver.page_source
    except DynamicUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - any driver failure degrades
        raise DynamicUnavailable(f"dynamic scenario failed: {exc}") from exc
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass
