"""LLM router tests: fallback, key rotation, quotas, cache, JSON validation (M3)."""
from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from buyerintel.db.models import LlmCallLog
from buyerintel.llm import router as router_mod
from buyerintel.llm.router import LLMError, LLMRouter, _check_schema

CFG = {
    "providers": {
        "alpha": {
            "base_url": "https://alpha.test/v1", "enabled": True,
            "data_class": "public", "key_env": "ALPHA_API_KEY",
            "rpm": 5, "rpd": 10, "tpd": 1000,
            "models": {"default": "alpha-1", "bulk_extraction": "alpha-1"},
        },
        "beta": {
            "base_url": "https://beta.test/v1", "enabled": True,
            "data_class": "public", "key_env": "BETA_API_KEY",
            "models": {"default": "beta-1", "bulk_extraction": "beta-1"},
        },
        "local": {
            "base_url": "http://localhost:11434/v1", "enabled": True,
            "data_class": "private", "key_env": None,
            "models": {"default": "llama3.1:8b"},
        },
    },
    "task_routing": {
        "bulk_extraction": ["alpha", "beta"],
        "judging": ["alpha"],
        "private_task": ["local"],
    },
    "router": {"failure_cooldown_seconds": 300, "max_consecutive_failures": 2,
               "key_rotation": True, "cache_ttl_hours": 168},
}


@pytest.fixture()
def router(session, monkeypatch):
    r = LLMRouter(session, config=CFG)
    monkeypatch.setenv("ALPHA_API_KEY", "alpha-key-1")
    monkeypatch.setenv("ALPHA_API_KEY_2", "alpha-key-2")
    monkeypatch.setenv("BETA_API_KEY", "beta-key-1")
    return r


def _ok_response(content='{"ok": true}', tokens=7):
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content}}],
              "usage": {"prompt_tokens": 3, "completion_tokens": tokens}},
        request=httpx.Request("POST", "https://x"),
    )


def test_success_on_first_provider(router, monkeypatch):
    seen_auth = []

    def fake_post(url, *, headers=None, json=None, timeout=None):
        seen_auth.append(headers.get("Authorization"))
        return _ok_response("hello")

    monkeypatch.setattr(router_mod.httpx, "post", fake_post)
    text = router.complete("hi", task="bulk_extraction")
    router.session.commit()
    assert text == "hello"
    assert seen_auth == ["Bearer alpha-key-1"]
    assert router.stats.misses == 1 and router.stats.fallbacks == 0


def test_provider_failure_falls_back_without_job_loss(router, monkeypatch):
    def fake_post(url, *, headers=None, json=None, timeout=None):
        if url.startswith("https://alpha.test"):
            return httpx.Response(429, json={}, request=httpx.Request("POST", url))
        return _ok_response("from beta")

    monkeypatch.setattr(router_mod.httpx, "post", fake_post)
    text = router.complete("hi", task="bulk_extraction")
    router.session.commit()

    assert text == "from beta"
    assert router.stats.fallbacks == 1
    outcome = router.session.scalars(
        select(LlmCallLog).where(LlmCallLog.provider == "alpha")
    ).one()
    assert outcome.outcome == "rate_limited"


def test_key_rotation_round_robin(router, monkeypatch):
    seen = []

    def fake_post(url, *, headers=None, json=None, timeout=None):
        seen.append(headers.get("Authorization"))
        return _ok_response("ok")

    monkeypatch.setattr(router_mod.httpx, "post", fake_post)
    for i in range(2):
        router.complete(f"prompt {i}", task="judging")  # distinct prompts beat the cache
    router.session.commit()

    assert seen == ["Bearer alpha-key-1", "Bearer alpha-key-2"]


def test_rpm_quota_skips_provider_and_falls_back(router, monkeypatch):
    def fake_post(url, *, headers=None, json=None, timeout=None):
        return _ok_response("ok")

    monkeypatch.setattr(router_mod.httpx, "post", fake_post)
    # saturate alpha's rpm window manually (limit 5/min), then the chain moves on
    import time as _time
    from collections import deque

    now = _time.monotonic()
    router._state("alpha").quota.minute_calls = deque([now] * 5)
    text = router.complete("hi", task="bulk_extraction")
    router.session.commit()
    assert text == "ok"  # beta served it - quota stop never loses the job
    assert router.stats.fallbacks == 1


def test_rpd_quota_exhaustion_raises_no_provider(router, monkeypatch):
    def fake_post(url, *, headers=None, json=None, timeout=None):
        return _ok_response("ok")

    monkeypatch.setattr(router_mod.httpx, "post", fake_post)
    state = router._state("alpha")
    state.quota.roll_day()
    state.quota.day_calls = 10  # rpd limit is 10
    router._state("alpha").quota.day_calls = 10
    with pytest.raises(LLMError):
        router.complete("hi", task="judging")  # only alpha in judging chain


def test_prompt_hash_cache_hit(router, monkeypatch):
    calls = []

    def fake_post(url, *, headers=None, json=None, timeout=None):
        calls.append(url)
        return _ok_response("cached-content")

    monkeypatch.setattr(router_mod.httpx, "post", fake_post)
    first = router.complete("same prompt", task="judging")
    router.session.commit()
    second = router.complete("same prompt", task="judging")
    router.session.commit()

    assert calls == ["https://alpha.test/v1/chat/completions"]  # one network call
    assert first == second == "cached-content"
    assert router.stats.cache_hits == 1
    assert router.stats.hit_rate == 0.5


def test_private_data_class_never_reaches_public_providers(router, monkeypatch):
    called_urls = []

    def fake_post(url, *, headers=None, json=None, timeout=None):
        called_urls.append(url)
        return _ok_response("local-ok")

    monkeypatch.setattr(router_mod.httpx, "post", fake_post)
    text = router.complete("secret prices", task="private_task", data_class="private")
    router.session.commit()

    assert text == "local-ok"
    # only the private (local ollama) provider was ever contacted
    assert all(u.startswith("http://localhost:11434") for u in called_urls)
    # and the chain itself contains only the private provider
    assert router._chain("private_task", data_class="private") == ["local"]


def test_complete_json_validates_and_retries(router, monkeypatch):
    replies = [
        'not json at all',
        '{"name": "x", "score": "not-a-number"}',
        '{"name": "x", "score": 5}',
    ]

    def fake_post(url, *, headers=None, json=None, timeout=None):
        return _ok_response(replies.pop(0))

    monkeypatch.setattr(router_mod.httpx, "post", fake_post)
    result = router.complete_json(
        "extract", task="bulk_extraction",
        schema={"type": "object", "required": ["name", "score"],
                "properties": {"name": {"type": "string"},
                               "score": {"type": "number"}}},
    )
    router.session.commit()
    assert result == {"name": "x", "score": 5}
    assert router.stats.validation_retries == 2


def test_check_schema_basics():
    schema = {"type": "object", "required": ["a"],
              "properties": {"a": {"type": "integer"},
                             "b": {"type": "array", "items": {"type": "string"}}}}
    assert _check_schema({"a": 1, "b": ["x"]}, schema) == []
    errors = _check_schema({"a": "one"}, schema)
    assert any("a" in e for e in errors)
    assert _check_schema({}, schema)  # missing required -> errors, no crash


def test_all_keys_missing_raises_clean(router, monkeypatch):
    monkeypatch.delenv("ALPHA_API_KEY", raising=False)
    monkeypatch.delenv("ALPHA_API_KEY_2", raising=False)
    monkeypatch.delenv("BETA_API_KEY", raising=False)
    with pytest.raises(LLMError) as excinfo:
        router.complete("hi", task="judging")
    assert "no API key" in str(excinfo.value)
