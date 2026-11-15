"""Challenge adapters (ADD-010/011) and LLM catalog sync tests (M3)."""
from __future__ import annotations

import httpx
import pytest

from buyerintel.enrich import challenges
from buyerintel.llm import catalog as catalog_mod


# ----------------------------------------------------------- flaresolverr --
def test_flaresolverr_disabled_by_default(monkeypatch):
    monkeypatch.setattr(challenges, "load_yaml", lambda name: {
        "escalation": {"flaresolverr": {"enabled": False}}})
    assert challenges.flaresolverr_fetch("https://example.com") is None


def test_flaresolverr_solves_and_returns_solution(monkeypatch):
    monkeypatch.setattr(challenges, "load_yaml", lambda name: {
        "escalation": {"flaresolverr": {"enabled": True, "url": "http://localhost:8191"}}})
    posted = {}

    def fake_post(url, *, json=None, timeout=None):
        posted["url"] = url
        posted["cmd"] = json["cmd"]
        return httpx.Response(200, json={
            "status": "ok",
            "solution": {"cookies": [], "userAgent": "x", "html": "<html/>"},
        }, request=httpx.Request("POST", url))

    monkeypatch.setattr(challenges.httpx, "post", fake_post)
    solution = challenges.flaresolverr_fetch("https://example.com/challenge")
    assert solution["userAgent"] == "x"
    assert posted["cmd"] == "request.get"


def test_flaresolverr_failure_raises(monkeypatch):
    monkeypatch.setattr(challenges, "load_yaml", lambda name: {
        "escalation": {"flaresolverr": {"enabled": True, "url": "http://localhost:8191"}}})

    def fake_post(url, *, json=None, timeout=None):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(challenges.httpx, "post", fake_post)
    with pytest.raises(challenges.FlareSolverrError):
        challenges.flaresolverr_fetch("https://example.com")


# ------------------------------------------------------------- nopecha ----
def test_captcha_disabled_by_default(monkeypatch):
    monkeypatch.setattr(challenges, "load_yaml", lambda name: {
        "escalation": {"captcha_solver": {"enabled": False}}})
    assert challenges.solve_captcha("sitekey", "https://example.com") is None


def test_captcha_daily_cap_enforced(monkeypatch):
    monkeypatch.setattr(challenges, "load_yaml", lambda name: {
        "escalation": {"captcha_solver": {"enabled": True, "daily_cap": 1}}})
    challenges._DAILY_CALLS["count"] = 5
    with pytest.raises(challenges.CaptchaError):
        challenges.solve_captcha("sk", "https://example.com")
    challenges._DAILY_CALLS["count"] = 0


# --------------------------------------------------------- catalog sync ---
def _fixture_catalog():
    return [
        {"name": "Groq", "url": "https://api.groq.com/openai/v1", "rpm": 45, "rpd": 14400},
        {"name": "Totally New Provider", "url": "https://new.test/v1"},
    ]


def test_merge_reports_quota_updates_and_new():
    ours = {"groq": {"base_url": "https://api.groq.com/openai/v1", "rpm": 30, "rpd": 1000}}
    report = catalog_mod.merge_catalog(_fixture_catalog(), our_providers=ours)
    assert "groq" in report["matched"]
    assert report["quota_updates"][0]["provider"] == "groq"
    assert report["quota_updates"][0]["changes"]["rpm"]["catalog"] == 45
    assert any("Totally New Provider" in n for n in report["new"])


def test_sync_writes_snapshot_without_touching_yaml(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog_mod, "fetch_catalog", lambda *a, **kw: _fixture_catalog())
    monkeypatch.setattr(catalog_mod, "DATA_DIR", tmp_path)
    report = catalog_mod.sync_catalog()
    snapshot = (tmp_path / "llm_catalog_snapshot.json").read_text(encoding="utf-8")
    assert "groq" in snapshot
    assert report["fetched_entries"] == 2
