"""Translation (spec section 8): offline first, LLM second.

Engine order and activation live in `config/translation.yaml`; the default is
`none` (no translation happens) until the user installs Argos Translate or
points LibreTranslate at a self-hosted instance.
"""
from __future__ import annotations

import httpx

from buyerintel.config import load_yaml


def _config() -> dict:
    return load_yaml("translation.yaml")


def translate(text: str, target_language: str, *, llm_router=None) -> str | None:
    """Translate `text` to a target language code (e.g. 'de').

    Returns None when no engine is configured or every engine fails - the
    caller falls back to the English query, never blocks.
    """
    cfg = _config()
    engine = str(cfg.get("engine") or "none").lower()
    if engine == "none" or not text:
        return None
    if engine == "argos":
        return _argos(text, target_language)
    if engine == "libretranslate":
        return _libretranslate(text, target_language, cfg)
    if engine == "llm":
        if llm_router is None:
            return None
        try:
            return llm_router.complete(
                f"Translate to {target_language}. Reply with the translation only:\n{text}",
                task="multilingual",
                max_tokens=256,
            )
        except Exception:  # noqa: BLE001 - translation must never block a search
            return None
    return None


def _argos(text: str, target_language: str) -> str | None:
    try:
        import argostranslate.translate  # heavy, optional install
    except ImportError:
        return None
    try:
        return argostranslate.translate.translate("en", target_language, text)
    except Exception:  # noqa: BLE001 - missing language pack etc.
        return None


def _libretranslate(text: str, target_language: str, cfg: dict) -> str | None:
    base = str(cfg.get("libretranslate_url") or "").rstrip("/")
    if not base:
        return None
    try:
        response = httpx.post(
            f"{base}/translate",
            json={"q": text, "source": "en", "target": target_language, "format": "text"},
            timeout=30,
        )
        response.raise_for_status()
        return response.json().get("translatedText")
    except (httpx.HTTPError, ValueError):
        return None
