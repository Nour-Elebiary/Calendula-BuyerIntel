"""Search router tests: simulated engine failures, fallback, cache hit rate (M3)."""
from __future__ import annotations

import pytest

from buyerintel.search import router as router_mod
from buyerintel.search.router import (
    EngineError,
    EngineHealth,
    SearchRouter,
    cache_key,
)
from datetime import timezone

from buyerintel.db.models import SearchCache


@pytest.fixture()
def fast_router(session, monkeypatch):
    """Router with politeness delays removed and adapters monkeypatchable."""
    monkeypatch.setattr(router_mod.time, "sleep", lambda s: None)
    return SearchRouter(session, config={
        "engines": {
            "duckduckgo": {"enabled": True},
            "google": {"enabled": True},
            "searxng": {"enabled": True, "url": "http://localhost:9999"},
        },
        "politeness": {"default_delay_s": 0, "jitter_s": 0, "cache_ttl_minutes": 60},
        "escalation": {"flaresolverr": {"enabled": False},
                       "captcha_solver": {"enabled": False}},
    })


def test_cache_key_is_deterministic():
    assert cache_key("ddg", "q", {"a": 1}) == cache_key("ddg", "q", {"a": 1})
    assert cache_key("ddg", "q") != cache_key("google", "q")


def test_first_engine_success(fast_router, monkeypatch):
    calls = []

    def fake_ddg(query, *, max_results):
        calls.append("ddg")
        return [router_mod.SearchResult(engine="duckduckgo", title="t", url="u", snippet="s")]

    monkeypatch.setattr(router_mod, "_ddgs_search", fake_ddg)
    results, stats = fast_router.search("herb buyer germany")
    session_commit = fast_router.session
    session_commit.commit()

    assert calls == ["ddg"]
    assert len(results) == 1
    assert stats.misses == 1 and stats.fallbacks == 0


def test_simulated_failure_falls_back_to_next_engine(fast_router, monkeypatch):
    order = []

    def fake_ddg(query, *, max_results):
        order.append("ddg")
        raise EngineError("duckduckgo", "blocked", captcha=True)

    def fake_google(query, *, max_results, config):
        order.append("google")
        raise EngineError("google", "no key")

    def fake_searx(query, *, max_results, config):
        order.append("searx")
        return [router_mod.SearchResult(engine="searxng", title="t", url="u")]

    monkeypatch.setattr(router_mod, "_ddgs_search", fake_ddg)
    monkeypatch.setattr(router_mod, "_google_cse_search", fake_google)
    monkeypatch.setattr(router_mod, "_searxng_search", fake_searx)

    results, stats = fast_router.search("herb buyer germany")
    fast_router.session.commit()

    assert order == ["ddg", "google", "searx"]
    assert results[0].engine == "searxng"
    assert stats.fallbacks == 2
    # captcha failure cools the engine down
    assert fast_router._health("duckduckgo").blocked_until is not None


def test_all_engines_fail_marks_unhealthy_without_escalation(fast_router, monkeypatch):
    def fail(query, **kwargs):
        raise EngineError("x", "blocked")

    monkeypatch.setattr(router_mod, "_ddgs_search", fail)
    monkeypatch.setattr(router_mod, "_searxng_search", fail)
    monkeypatch.setattr(router_mod, "_google_cse_search", fail)

    results, stats = fast_router.search("q")
    fast_router.session.commit()

    assert results == []
    assert stats.unhealthy  # marked, not guessed
    assert stats.escalations == 0  # flaresolverr/captcha flags are off


def test_cache_hit_skips_engine_and_logs_hit_rate(fast_router, monkeypatch):
    calls = []

    def fake_ddg(query, *, max_results):
        calls.append(query)
        return [router_mod.SearchResult(engine="duckduckgo", title="t", url="u")]

    monkeypatch.setattr(router_mod, "_ddgs_search", fake_ddg)
    _, first_stats = fast_router.search("same query")
    fast_router.session.commit()
    _, second_stats = fast_router.search("same query")
    fast_router.session.commit()

    assert calls == ["same query"]            # second call served from cache
    assert second_stats.cache_hits == 1
    assert second_stats.hit_rate == 1.0
    assert first_stats.misses == 1


def test_unhealthy_engine_not_selected_until_cooldown_passes():
    now = router_mod.datetime.now(timezone.utc)
    health = EngineHealth()
    assert health.available(now)
    health.record_failure(now, captcha=True)
    assert not health.available(now)          # captcha cooldown
    assert health.score < 1.0


def test_escalation_rung_runs_only_when_enabled(session, monkeypatch):
    monkeypatch.setattr(router_mod.time, "sleep", lambda s: None)
    cfg = {
        "engines": {"duckduckgo": {"enabled": True}},
        "politeness": {"default_delay_s": 0, "jitter_s": 0},
        "escalation": {"flaresolverr": {"enabled": True, "url": "http://localhost:8191"}},
    }
    router = SearchRouter(session, config=cfg)

    def fail(query, *, max_results):
        raise EngineError("duckduckgo", "challenge", captcha=True)

    def fake_fs(url, *, timeout=75.0):
        return {"status": "ok", "solution": {"html": "<html>ok</html>"}}

    monkeypatch.setattr(router_mod, "_ddgs_search", fail)
    import buyerintel.enrich.challenges as challenges

    monkeypatch.setattr(challenges, "flaresolverr_fetch", fake_fs)
    results, stats = router.search("q")
    session.commit()
    assert stats.escalations == 1


def test_engine_without_adapter_is_skipped(session):
    router = SearchRouter(session, config={
        "engines": {"baidu": {"enabled": True}, "duckduckgo": {"enabled": True}},
        "politeness": {"default_delay_s": 0, "jitter_s": 0},
    })
    order = router._engine_order(type("Q", (), {"engines": []})(), country="CHN")
    assert "baidu" not in order       # registered but not wired -> skipped
    assert "duckduckgo" in order
