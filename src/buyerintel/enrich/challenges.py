"""Challenge/CAPTCHA escalation adapters (addendum 4.8, ADD-010 / ADD-011).

Rung 5: FlareSolverr (headless-browser proxy for Cloudflare-style challenges).
Rung 6: CAPTCHA solver (NopeCHA first), last resort, public result pages only.

Both are OFF by default (config/engines.yaml escalation flags). They never
receive logged-in sessions, LinkedIn login adapters, or API traffic carrying
keys (addendum 4.8 ladder rules).
"""
from __future__ import annotations

import datetime as _dt

import httpx

from buyerintel.config import get_secret, load_yaml


class FlareSolverrError(Exception):
    pass


def flaresolverr_fetch(url: str, *, timeout: float = 75.0) -> dict | None:
    """Solve a Cloudflare-style challenge for `url` via a local FlareSolverr.

    Returns the solution dict (cookies, userAgent, html) or None on refusal.
    Raises FlareSolverrError when the service is enabled but unreachable.
    """
    cfg = (load_yaml("engines.yaml").get("escalation") or {}).get("flaresolverr") or {}
    if not cfg.get("enabled"):
        return None
    base = (cfg.get("url") or "http://localhost:8191").rstrip("/")
    try:
        response = httpx.post(
            f"{base}/v1",
            json={"cmd": "request.get", "url": url, "maxTimeout": int(timeout * 1000)},
            timeout=timeout + 15,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise FlareSolverrError(f"flaresolverr unreachable: {exc}") from exc
    payload = response.json()
    if payload.get("status") != "ok":
        return None
    return payload.get("solution") or {}


class CaptchaError(Exception):
    pass


_DAILY_CALLS = {"date": None, "count": 0}


def _daily_cap_reached(daily_cap: int) -> bool:
    today = _dt.date.today()
    if _DAILY_CALLS["date"] != today:
        _DAILY_CALLS["date"] = today
        _DAILY_CALLS["count"] = 0
    return _DAILY_CALLS["count"] >= daily_cap


def solve_captcha(site_key: str, page_url: str, *, captcha_type: str = "hcaptcha") -> str | None:
    """NopeCHA API solve (rung 6). Returns the token or None; enforces the daily cap.

    `page_url` must be a public result page - never a logged-in or API URL.
    """
    cfg = (load_yaml("engines.yaml").get("escalation") or {}).get("captcha_solver") or {}
    if not cfg.get("enabled"):
        return None
    daily_cap = int(cfg.get("daily_cap", 100))
    if _daily_cap_reached(daily_cap):
        raise CaptchaError(f"daily CAPTCHA cap of {daily_cap} reached")
    api_key = get_secret("nopecha_api_key")
    if not api_key:
        raise CaptchaError("no nopecha key configured (config/secrets.yaml)")
    response = httpx.post(
        "https://api.nopecha.com/token/",
        json={"type": captcha_type, "sitekey": site_key, "url": page_url, "key": api_key},
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    _DAILY_CALLS["count"] += 1
    return data.get("data") or data.get("token") or None
