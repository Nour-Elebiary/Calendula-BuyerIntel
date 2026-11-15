"""LLM router (spec section 9).

OpenAI-compatible chat-completions over httpx with:
- per-provider quota tracking (RPM / RPD / TPD rolling counters),
- multi-key round-robin per provider (`<KEY_ENV>_2`, `_3`, ... - the Gemini
  key-rotation pattern),
- prompt-hash response caching (`llm_cache`),
- JSON output validation with corrective retries, then fallback,
- a fallback chain per task from `providers.yaml task_routing`,
- data-class enforcement: private content only ever reaches private providers,
- health tracking with cooldowns, logged per call in `llm_call_log`.

Never a single provider: every call can fall back without losing the job.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.config import get_secret, load_yaml
from buyerintel.db.models import LlmCache, LlmCallLog

USER_AGENT = "Calendula-BuyerIntel/0.1 (local personal research)"


class LLMError(Exception):
    """All providers failed or produced unusable output."""


class ProviderError(Exception):
    """One provider failed (network, quota, bad output)."""


def prompt_hash(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


@dataclass
class QuotaState:
    """Rolling quota counters for one provider."""

    minute_calls: deque = field(default_factory=deque)
    day_calls: int = 0
    day_date: date | None = None
    day_tokens: int = 0

    def roll_day(self) -> None:
        today = date.today()
        if self.day_date != today:
            self.day_date = today
            self.day_calls = 0
            self.day_tokens = 0

    def rpm_ok(self, limit: int) -> bool:
        now = time.monotonic()
        while self.minute_calls and now - self.minute_calls[0] > 60:
            self.minute_calls.popleft()
        return len(self.minute_calls) < limit

    def record_call(self, rpm_limit: int, tokens: int) -> None:
        self.roll_day()
        now = time.monotonic()
        self.minute_calls.append(now)
        while self.minute_calls and now - self.minute_calls[0] > 60:
            self.minute_calls.popleft()
        self.day_calls += 1
        self.day_tokens += tokens


@dataclass
class ProviderState:
    quota: QuotaState = field(default_factory=QuotaState)
    consecutive_failures: int = 0
    cooldown_until: datetime | None = None
    key_index: int = 0

    def available(self, cooldown_seconds: int) -> bool:
        if self.cooldown_until is None:
            return True
        if datetime.now(timezone.utc) >= self.cooldown_until:
            self.cooldown_until = None
            self.consecutive_failures = 0
            return True
        return False

    def record_failure(self, cooldown_seconds: int, max_failures: int) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= max_failures:
            self.cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=cooldown_seconds)
            self.consecutive_failures = 0

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.cooldown_until = None

    def next_key_index(self) -> int:
        index = self.key_index
        self.key_index += 1
        return index


@dataclass
class RouterStats:
    calls: int = 0
    cache_hits: int = 0
    misses: int = 0
    fallbacks: int = 0
    validation_retries: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.cache_hits + self.misses
        return round(self.cache_hits / total, 3) if total else 0.0

    def summary(self) -> str:
        return (
            f"llm calls={self.calls} cache_hits={self.cache_hits} hit_rate={self.hit_rate} "
            f"fallbacks={self.fallbacks} validation_retries={self.validation_retries}"
        )


def _check_schema(value, schema: dict, path: str = "$") -> list[str]:
    """Minimal JSON-schema subset validation: type + required + properties.

    Supports the shapes this project's extraction tasks need; full JSON Schema
    is deliberately not implemented (spec asks for validation, not a spec impl).
    """
    errors: list[str] = []
    expected = schema.get("type")
    type_map = {
        "object": dict, "array": list,
        "string": str, "number": (int, float),
        "integer": int, "boolean": bool, "null": type(None),
    }
    if expected and expected in type_map and not isinstance(value, type_map[expected]):
        errors.append(f"{path}: expected {expected}, got {type(value).__name__}")
        return errors
    if isinstance(value, dict) and expected == "object":
        for required in schema.get("required", []):
            if required not in value:
                errors.append(f"{path}: missing required '{required}'")
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                errors.extend(_check_schema(value[key], sub, f"{path}.{key}"))
    if isinstance(value, list) and expected == "array":
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(value):
                errors.extend(_check_schema(item, item_schema, f"{path}[{i}]"))
    return errors


class LLMRouter:
    """Task-routed, quota-aware, key-rotating, cached LLM access."""

    def __init__(self, session: Session, config: dict | None = None):
        self.session = session
        cfg = config or load_yaml("providers.yaml")
        self.providers: dict[str, dict] = cfg.get("providers") or {}
        self.task_routing: dict[str, list[str]] = cfg.get("task_routing") or {}
        router_cfg = cfg.get("router") or {}
        self.cooldown_seconds = int(router_cfg.get("failure_cooldown_seconds", 300))
        self.max_consecutive_failures = int(router_cfg.get("max_consecutive_failures", 2))
        self.key_rotation = bool(router_cfg.get("key_rotation", True))
        self.cache_ttl_hours = int(router_cfg.get("cache_ttl_hours", 168))
        self.state: dict[str, ProviderState] = {}
        self.stats = RouterStats()

    # ------------------------------------------------------------------ keys
    def keys_for(self, provider: str) -> list[str]:
        """All configured keys for a provider: KEY_ENV, KEY_ENV_2, ..."""
        entry = self.providers.get(provider) or {}
        base = entry.get("key_env")
        if not base:
            return []  # local providers need no key
        keys = []
        if get_secret(base):
            keys.append(get_secret(base))
        if self.key_rotation:
            for i in range(2, 20):
                key = get_secret(f"{base}_{i}")
                if key:
                    keys.append(key)
                else:
                    break
        return keys

    def _state(self, provider: str) -> ProviderState:
        return self.state.setdefault(provider, ProviderState())

    # ------------------------------------------------------------------ chain
    def _chain(self, task: str, *, data_class: str) -> list[str]:
        names = list(self.task_routing.get(task) or [])
        if not names:
            names = [p for p, e in self.providers.items() if e.get("enabled")]
        allowed: list[str] = []
        for name in names:
            entry = self.providers.get(name) or {}
            if not entry.get("enabled", False):
                continue
            if data_class == "private" and entry.get("data_class") != "private":
                continue  # private content never reaches public providers
            allowed.append(name)
        return allowed

    def _pick_model(self, provider: str, task: str) -> str:
        entry = self.providers.get(provider) or {}
        models = entry.get("models") or {}
        return str(models.get(task) or models.get("default") or "")

    def _base_url(self, provider: str) -> str:
        entry = self.providers.get(provider) or {}
        url = str(entry.get("base_url") or "")
        if "{account_id}" in url:
            account_id = get_secret("cloudflare_account_id")
            if not account_id:
                raise ProviderError("cloudflare: no account id configured")
            url = url.replace("{account_id}", account_id)
        return url.rstrip("/")

    # ------------------------------------------------------------------ cache
    def _cache_get(self, provider: str, model: str, p_hash: str) -> LlmCache | None:
        row = self.session.scalar(
            select(LlmCache).where(
                LlmCache.provider == provider,
                LlmCache.model == model,
                LlmCache.prompt_hash == p_hash,
            )
        )
        if row is None:
            return None
        created = row.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - created > timedelta(hours=self.cache_ttl_hours):
            return None
        return row

    def _cache_put(self, provider: str, model: str, p_hash: str, task: str,
                   text: str, tokens_in: int, tokens_out: int) -> None:
        self.session.merge(
            LlmCache(
                provider=provider,
                model=model,
                prompt_hash=p_hash,
                task=task,
                response_text=text,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                created_at=datetime.now(timezone.utc),
            )
        )
        self.session.flush()

    # ------------------------------------------------------------------- call
    def complete(self, prompt: str, *, task: str = "default", system: str | None = None,
                 data_class: str = "public", max_tokens: int = 1024,
                 temperature: float = 0.2) -> str:
        """Complete a prompt through the task's provider chain with fallbacks."""
        chain = self._chain(task, data_class=data_class)
        if not chain:
            raise LLMError(
                f"no enabled provider for task '{task}' (data_class={data_class})"
            )
        last_error: Exception | None = None
        for provider in chain:
            try:
                text = self._complete_one(provider, prompt, task=task, system=system,
                                          max_tokens=max_tokens, temperature=temperature)
            except ProviderError as exc:
                last_error = exc
                self.stats.fallbacks += 1
                continue
            return text
        raise LLMError(f"all providers failed for task '{task}': {last_error}")

    def _complete_one(self, provider: str, prompt: str, *, task: str, system: str | None,
                      max_tokens: int, temperature: float) -> str:
        state = self._state(provider)
        entry = self.providers.get(provider) or {}
        model = self._pick_model(provider, task)
        p_hash = prompt_hash(provider, model, system or "", prompt)

        self.stats.calls += 1
        cached = self._cache_get(provider, model, p_hash)
        if cached is not None:
            self.stats.cache_hits += 1
            self._log(provider, model, task, p_hash, cached.tokens_in,
                      cached.tokens_out, 0, "cache_hit")
            return cached.response_text
        self.stats.misses += 1

        # Quota gates (RPM / RPD / TPD) - skip provider, never block the chain.
        quota_cfg = {k: entry.get(k) for k in ("rpm", "rpd", "tpd") if entry.get(k)}
        state.quota.roll_day()
        if quota_cfg.get("rpm") and not state.quota.rpm_ok(int(quota_cfg["rpm"])):
            raise ProviderError(f"{provider}: rpm limit {quota_cfg['rpm']} reached")
        if quota_cfg.get("rpd") and state.quota.day_calls >= int(quota_cfg["rpd"]):
            raise ProviderError(f"{provider}: rpd limit reached")
        if quota_cfg.get("tpd") and state.quota.day_tokens >= int(quota_cfg["tpd"]):
            raise ProviderError(f"{provider}: tpd limit reached")

        keys = self.keys_for(provider)
        if entry.get("key_env") and not keys:
            raise ProviderError(f"{provider}: no API key configured")
        key = keys[state.next_key_index() % len(keys)] if keys else None

        headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload = {
            "model": model,
            "messages": ([{"role": "system", "content": system}] if system else [])
            + [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        started = time.monotonic()
        try:
            response = httpx.post(
                f"{self._base_url(provider)}/chat/completions",
                headers=headers,
                json=payload,
                timeout=120,
            )
        except httpx.HTTPError as exc:
            state.record_failure(self.cooldown_seconds, self.max_consecutive_failures)
            self._log(provider, model, task, p_hash, None, None,
                      int((time.monotonic() - started) * 1000), "error")
            raise ProviderError(f"{provider}: {exc}") from exc
        latency = int((time.monotonic() - started) * 1000)
        if response.status_code in (429, 402, 403):
            state.record_failure(self.cooldown_seconds, self.max_consecutive_failures)
            self._log(provider, model, task, p_hash, None, None, latency, "rate_limited")
            raise ProviderError(f"{provider}: status {response.status_code}")
        response.raise_for_status()
        try:
            body = response.json()
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            state.record_failure(self.cooldown_seconds, self.max_consecutive_failures)
            self._log(provider, model, task, p_hash, None, None, latency, "bad_response")
            raise ProviderError(f"{provider}: unusable response ({exc})") from exc
        usage = body.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)
        state.quota.record_call(int(quota_cfg.get("rpm") or 60), tokens_in + tokens_out)
        state.record_success()
        self._cache_put(provider, model, p_hash, task, text, tokens_in, tokens_out)
        self._log(provider, model, task, p_hash, tokens_in, tokens_out, latency, "ok")
        return text

    def complete_json(self, prompt: str, *, schema: dict, task: str = "bulk_extraction",
                      data_class: str = "public", system: str | None = None,
                      max_tokens: int = 2048) -> dict:
        """JSON output with schema validation, corrective retries, then fallback."""
        instruction = (
            f"{prompt}\n\nRespond with ONLY valid JSON matching this shape:\n"
            f"{json.dumps(schema)}\nNo markdown, no commentary."
        )
        last_error: Exception | None = None
        chain = self._chain(task, data_class=data_class)
        if not chain:
            raise LLMError(f"no enabled provider for task '{task}'")
        for provider in chain:
            for attempt in range(2):
                try:
                    text = self._complete_one(provider, instruction, task=task,
                                              system=system, max_tokens=max_tokens,
                                              temperature=0.0 if attempt == 0 else 0.2)
                except ProviderError as exc:
                    last_error = exc
                    self.stats.fallbacks += 1
                    break  # next provider
                parsed = _extract_json(text)
                if parsed is None:
                    last_error = ProviderError(f"{provider}: no JSON object in response")
                    self.stats.validation_retries += 1
                    instruction += (
                        "\nYour previous reply was not parseable JSON. "
                        "Respond again with ONLY the JSON object."
                    )
                    continue
                errors = _check_schema(parsed, schema)
                if not errors:
                    return parsed
                last_error = ProviderError(
                    f"{provider}: schema violations: {'; '.join(errors[:3])}"
                )
                self.stats.validation_retries += 1
                instruction += (
                    "\nYour previous JSON violated the required shape. Fix these fields: "
                    + "; ".join(errors[:3])
                )
        raise LLMError(f"complete_json failed for task '{task}': {last_error}")

    # -------------------------------------------------------------------- log
    def _log(self, provider: str, model: str, task: str, p_hash: str,
             tokens_in: int | None, tokens_out: int | None,
             latency_ms: int, outcome: str) -> None:
        self.session.add(
            LlmCallLog(
                provider=provider,
                model=model,
                task=task,
                prompt_hash=p_hash,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency_ms,
                outcome=outcome,
            )
        )
        self.session.flush()

    # ----------------------------------------------------------------- health
    def health(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        rows = []
        for name, entry in self.providers.items():
            state = self.state.get(name)
            quota = state.quota if state else QuotaState()
            rows.append(
                {
                    "provider": name,
                    "enabled": bool(entry.get("enabled")),
                    "has_key": bool(self.keys_for(name)) or not entry.get("key_env"),
                    "consecutive_failures": state.consecutive_failures if state else 0,
                    "cooling_down": bool(state and state.cooldown_until
                                         and state.cooldown_until > now),
                    "day_calls": quota.day_calls,
                    "day_tokens": quota.day_tokens,
                    "data_class": entry.get("data_class"),
                }
            )
        return rows


def _extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of an LLM reply (tolerates code fences)."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    start = cleaned.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(cleaned)):
        if cleaned[i] == "{":
            depth += 1
        elif cleaned[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(cleaned[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None
