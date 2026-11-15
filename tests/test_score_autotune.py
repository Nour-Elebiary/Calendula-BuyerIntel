"""Auto-tune tests (spec section 13 end, M5).

Pure-python L2 logistic regression; 50/50 blend with prior; +-40% bounds;
versioned weight sets; dormant below 200 labelled leads.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from buyerintel.score import autotune
from buyerintel.score.autotune import fit_logistic, labelled_outcomes, tune


def _real_weights() -> Path:
    from buyerintel.config import CONFIG_DIR
    return CONFIG_DIR / "weights.yaml"


def test_fit_logistic_separates_separable_data():
    """Component 0 high -> label 1; low -> label 0. The fit must rank it up."""
    X = [[float(i % 2) * 30.0, 5.0] for i in range(40)]
    y = [i % 2 for i in range(40)]
    w = fit_logistic(X, y, epochs=2000)
    weight_component0 = w[1]
    assert weight_component0 > 0.1, "positive correlation must yield a positive weight"


def test_labelled_outcomes_reads_tags(session):
    from buyerintel.db.models import Company

    session.add(Company(canonical_name="A", tags=["outcome:order"]))
    session.add(Company(canonical_name="B", tags=["outcome:reply"]))
    session.add(Company(canonical_name="C", tags=["outcome:none"]))  # not an outcome
    session.add(Company(canonical_name="D"))                          # unlabelled
    session.commit()
    labels = labelled_outcomes(session)
    assert labels == {1: 1, 2: 1}


def test_tune_dormant_below_threshold(session):
    report = tune(session)
    assert report["ran"] is False
    assert "200" in report["reason"]


def test_tune_blends_bounds_and_versions(session, monkeypatch, tmp_path):
    """With enough labelled leads: blend 50/50, respect +-40%, bump version,
    archive the prior, and renormalize to 100."""
    from buyerintel.catalog import seed_products
    from buyerintel.db.models import Company, ScoreRun
    from buyerintel.score.personas import sync_personas
    from buyerintel.score.scoring import score_company

    seed_products(session)
    sync_personas(session)
    session.commit()

    monkeypatch.setattr(autotune, "MIN_LABELLED", 10)
    monkeypatch.setattr(autotune, "CONFIG_DIR", tmp_path)
    # the redirected config dir starts with a copy of the real weights file
    import shutil

    shutil.copy(_real_weights(), tmp_path / "weights.yaml")

    # 20 companies: strong buyers get outcome:order, weak ones stay negative.
    for i in range(20):
        strong = i % 2 == 0
        company = Company(
            canonical_name=f"Co {i}",
            tags=(["spice importer", "bulk", "hibiscus", "organic", "halal",
                   "outcome:order" if strong else "outcome:none"]),
        )
        session.add(company)
        session.commit()
        score_company(session, company)
    session.commit()

    prior_version, prior_weights, thresholds = autotune.scoring_load()
    report = tune(session)
    assert report["ran"] is True
    assert report["to_version"] == prior_version + 1
    new = report["new_weights"]
    assert set(new) == set(prior_weights)
    # +-40% bounds
    for name, value in new.items():
        assert 0.6 * prior_weights[name] - 0.01 <= value <= 1.4 * prior_weights[name] + 0.01
    assert abs(sum(new.values()) - 100.0) < 0.5
    # prior archived, new weights written
    assert (tmp_path / "weights_history" / f"v{prior_version}.yaml").exists()
    written = (tmp_path / "weights.yaml").read_text(encoding="utf-8")
    assert f"version: {prior_version + 1}" in written
