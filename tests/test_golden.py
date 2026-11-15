from __future__ import annotations

import json
from pathlib import Path

import pytest

from buyerintel.resolve.dedupe import decide_pair
from buyerintel.resolve.matchers import (
    MatchKeys,
    normalize_domain,
    normalize_name,
    normalize_phone,
    normalize_registry,
    normalize_social,
)

GOLDEN = json.loads(
    (Path(__file__).parent / "fixtures" / "golden_dedupe.json").read_text(encoding="utf-8")
)

EXPECTED_ACTION = {"match": "merge", "distinct": "distinct", "link": "link", "adjudicate": "adjudicate"}


def make_keys(entry: dict) -> MatchKeys:
    """Same normalization path as keys_from_company in production."""
    country = entry.get("country")
    return MatchKeys(
        company_id=None,
        domain=normalize_domain(entry.get("domain")),
        registries=[r for r in (normalize_registry(entry.get(k)) for k in ("registry_id", "vat_id", "lei")) if r],
        phones=[p for p in (normalize_phone(entry.get("phone"), country),) if p],
        socials=[s for s in (normalize_social(entry.get("social")),) if s],
        name_key=normalize_name(entry.get("name", "")),
        country=country,
        address=entry.get("address"),
    )


@pytest.mark.parametrize("pair", GOLDEN["pairs"], ids=lambda p: f"{p['a'].get('name', '?')} vs {p['b'].get('name', '?')}")
def test_golden_pair(pair):
    decision = decide_pair(make_keys(pair["a"]), make_keys(pair["b"]))
    assert decision.action == EXPECTED_ACTION[pair["label"]], (
        f"got {decision.action} ({decision.reason}, sim={decision.score:.3f}) "
        f"for label {pair['label']}: {pair['a']['name']} vs {pair['b']['name']}"
    )


def test_golden_precision_and_recall():
    """M1 acceptance: >=0.98 precision, >=0.90 recall on the golden dedupe set."""
    tp = fp = fn = 0
    for pair in GOLDEN["pairs"]:
        decision = decide_pair(make_keys(pair["a"]), make_keys(pair["b"]))
        merged = decision.action == "merge"
        truth = pair["label"] == "match"
        if merged and truth:
            tp += 1
        elif merged and not truth:
            fp += 1
        elif truth and not merged:
            fn += 1
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    assert precision >= 0.98, f"precision {precision:.3f} below 0.98 (fp={fp})"
    assert recall >= 0.90, f"recall {recall:.3f} below 0.90 (fn={fn})"
