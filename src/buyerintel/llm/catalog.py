"""Provider catalog sync (spec section 9).

Periodically pulls `data.json` from the mnfst/awesome-free-llm-apis repo,
normalizes the entries, and writes a snapshot to `data/llm_catalog_snapshot.json`.
The snapshot never overwrites `config/providers.yaml` (hand-maintained, with
comments); the sync output is a report of quota/base-url changes to merge
manually or review via `buyerintel llm-sync-catalog`.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from buyerintel.config import DATA_DIR, load_yaml

RAW_URL = "https://raw.githubusercontent.com/mnfst/awesome-free-llm-apis/main/data.json"


def fetch_catalog(url: str = RAW_URL, *, timeout: float = 30.0) -> list[dict]:
    response = httpx.get(url, timeout=timeout, headers={"User-Agent": "Calendula-BuyerIntel/0.1"})
    response.raise_for_status()
    payload = response.json()
    # The file has been both a bare list and {"apis": [...]} - handle both.
    if isinstance(payload, dict):
        payload = payload.get("apis") or payload.get("data") or []
    return [entry for entry in payload if isinstance(entry, dict)]


def _match_name(entry: dict, our_providers: dict) -> str | None:
    """Match a catalog entry to one of our provider slugs (name/URL heuristics)."""
    text = " ".join(
        str(entry.get(key) or "")
        for key in ("name", "provider", "url", "base_url", "endpoint", "host")
    ).lower()
    for slug, entry_cfg in our_providers.items():
        base = str(entry_cfg.get("base_url") or "").lower()
        host = base.split("//")[-1].split("/")[0] if base else ""
        tokens = [slug, host] if host else [slug]
        if any(tok and tok in text for tok in tokens):
            return slug
    return None


def merge_catalog(catalog: list[dict], *, our_providers: dict | None = None) -> dict:
    """Diff the fetched catalog against our provider table.

    Returns {"matched": [...], "new": [...], "quota_updates": [...]}.
    Never mutates providers.yaml; the report is saved as a snapshot.
    """
    ours = our_providers or (load_yaml("providers.yaml").get("providers") or {})
    report: dict = {"matched": [], "new": [], "quota_updates": []}
    seen_slugs: set[str] = set()
    for entry in catalog:
        slug = _match_name(entry, ours)
        if slug:
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            report["matched"].append(slug)
            changes = {}
            for key, our_key in (("rpm", "rpm"), ("rpd", "rpd"), ("tpd", "tpd")):
                catalog_value = entry.get(key)
                our_value = ours.get(slug, {}).get(key)
                if catalog_value is not None and our_value is not None:
                    try:
                        cv = int(catalog_value)
                        ov = int(our_value)
                    except (TypeError, ValueError):
                        continue
                    if cv != ov:
                        changes[key] = {"ours": ov, "catalog": cv}
            if changes:
                report["quota_updates"].append({"provider": slug, "changes": changes})
        else:
            name = str(entry.get("name") or entry.get("provider") or "?")
            report["new"].append(name)
    return report


def sync_catalog(*, timeout: float = 30.0) -> dict:
    """Fetch, diff, snapshot. Returns the report and writes data/llm_catalog_snapshot.json."""
    catalog = fetch_catalog(timeout=timeout)
    report = merge_catalog(catalog)
    report["fetched_entries"] = len(catalog)
    snapshot_path = DATA_DIR / "llm_catalog_snapshot.json"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(
        json.dumps({"catalog": catalog, "report": report}, indent=2),
        encoding="utf-8",
    )
    return report
