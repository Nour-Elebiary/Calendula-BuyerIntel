"""Weight auto-tuning (spec section 13, end).

Dormant by design: tuning only runs once >= 200 outcome-labelled leads exist
(reply, sample requested, quote, order). Outcomes arrive with outreach
tracking (M8); until then ``labelled_outcomes`` finds none and ``tune()``
returns a no-op report.

Method, per spec:
- regularized (L2) logistic regression on the component scores -> fitted weights
- new = 0.5 * prior + 0.5 * fitted  (50/50 blend with the prior)
- each weight bounded to prior * [0.6, 1.4]  (+-40 percent)
- renormalized to sum 100, version bumped, prior archived to
  config/weights_history/v{n}.yaml
"""
from __future__ import annotations

import math
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.config import CONFIG_DIR, load_yaml
from buyerintel.db.models import Company, ScoreRun

OUTCOME_TAG = re.compile(r"^outcome:(reply|sample|quote|order)$")
POSITIVE_OUTCOMES = {"reply", "sample", "quote", "order"}

MIN_LABELLED = 200
L2 = 1.0
LEARNING_RATE = 0.1
EPOCHS = 800


def labelled_outcomes(session: Session) -> dict[int, int]:
    """company_id -> 1 (positive outcome) | 0 (engaged but negative).

    Source: ``outcome:<x>`` tags on companies (written by outreach tracking
    from M8 on). Tags for reply/sample/quote/order are positive; the absence
    of any outcome tag means unlabelled -> excluded from the fit.
    """
    out: dict[int, int] = {}
    for company in session.scalars(select(Company)).all():
        tags = [str(t).lower() for t in (company.tags or [])]
        hits = {m.group(1) for t in tags if (m := OUTCOME_TAG.match(t))}
        if hits:
            out[company.id] = 1 if hits & POSITIVE_OUTCOMES else 0
    return out


def training_set(session: Session) -> tuple[list[list[float]], list[int], list[str]]:
    """X (component vectors from the latest score_run per company), y labels."""
    outcomes = labelled_outcomes(session)
    if not outcomes:
        return [], [], []
    latest: dict[int, ScoreRun] = {}
    for run in session.scalars(
        select(ScoreRun).where(ScoreRun.company_id.in_(list(outcomes))).order_by(ScoreRun.id)
    ).all():
        latest[run.company_id] = run  # ascending ids: last wins
    X, y, names = [], [], []
    for company_id, run in latest.items():
        components = (run.components or {}).get("components") or {}
        if not names:
            names = sorted(components)
        X.append([float(components.get(n, 0.0)) for n in names])
        y.append(outcomes[company_id])
    return X, y, names


def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def fit_logistic(X: list[list[float]], y: list[int], *, l2: float = L2,
                 lr: float = LEARNING_RATE, epochs: int = EPOCHS) -> list[float]:
    """Pure-python L2-regularized logistic regression (batch gradient descent).

    Deterministic: no randomness, fixed epoch count. Weights include an
    intercept at index 0; callers strip it.
    """
    if not X:
        return []
    n_features = len(X[0]) + 1  # + intercept
    w = [0.0] * n_features
    n = len(X)
    for _ in range(epochs):
        grad = [0.0] * n_features
        for row, label in zip(X, y):
            z = w[0] + sum(wi * xi for wi, xi in zip(w[1:], row))
            err = _sigmoid(z) - label
            grad[0] += err
            for j, xi in enumerate(row, start=1):
                grad[j] += err * xi
        for j in range(n_features):
            reg = 0.0 if j == 0 else l2 * w[j] / max(1, n)
            w[j] -= lr * (grad[j] / max(1, n) + reg)
    return w


def tune(session: Session, *, current: dict | None = None) -> dict:
    """Run one tuning pass. Returns a report; no-op below MIN_LABELLED."""
    report: dict = {"ran": False, "labelled": 0, "reason": ""}
    version, weights, thresholds = scoring_load()
    if current:
        weights = current
    X, y, names = training_set(session)
    report["labelled"] = len(y)
    if len(y) < MIN_LABELLED:
        report["reason"] = f"need {MIN_LABELLED} labelled leads, have {len(y)}"
        return report
    if not names or sorted(names) != sorted(weights):
        report["reason"] = f"score components {names} do not match weights keys {sorted(weights)}"
        return report

    fitted = fit_logistic(X, y)[1:]  # strip intercept
    # Scale fitted weights to the prior's 0-100 world, then blend 50/50.
    fitted_positive = [max(w, 0.0) for w in fitted]
    fitted_sum = sum(fitted_positive) or 1.0
    fitted_scaled = {name: 100.0 * w / fitted_sum for name, w in zip(names, fitted_positive)}

    new_weights: dict[str, float] = {}
    bounds: dict[str, tuple[float, float]] = {}
    bounded = []
    for name, prior in weights.items():
        blended = 0.5 * prior + 0.5 * fitted_scaled.get(name, prior)
        lo, hi = 0.6 * prior, 1.4 * prior
        bounds[name] = (lo, hi)
        clamped = max(lo, min(hi, blended))
        if clamped != blended:
            bounded.append(name)
        new_weights[name] = clamped
    # Renormalize to exactly 100 WITHOUT leaving the +-40% bounds: only
    # weights not sitting on a bound absorb the correction, proportionally.
    for _ in range(100):
        total = sum(new_weights.values())
        if abs(total - 100.0) < 0.01:
            break
        free = [n for n, v in new_weights.items() if bounds[n][0] < v < bounds[n][1]]
        if not free:
            break  # every weight pinned: total drifts, reported below
        free_sum = sum(new_weights[n] for n in free)
        factor = (free_sum + (100.0 - total)) / free_sum
        if factor <= 0:
            break
        for n in free:
            new_weights[n] = max(bounds[n][0], min(bounds[n][1], new_weights[n] * factor))
    new_weights = {k: round(v, 2) for k, v in new_weights.items()}

    new_version = version + 1
    _archive_weights(version)
    _write_weights(new_version, new_weights, thresholds)

    report.update({"ran": True, "from_version": version, "to_version": new_version,
                   "bounded_weights": bounded, "new_weights": new_weights})
    return report


def scoring_load() -> tuple[int, dict, dict]:
    from buyerintel.score.scoring import load_weights
    return load_weights()


def _archive_weights(version: int) -> None:
    src = CONFIG_DIR / "weights.yaml"
    if not src.exists():
        return
    history = CONFIG_DIR / "weights_history"
    history.mkdir(parents=True, exist_ok=True)
    (history / f"v{version}.yaml").write_bytes(src.read_bytes())


def _write_weights(version: int, weights: dict, thresholds: dict) -> None:
    lines = [
        "# Scoring weights - AUTO-TUNED. Prior versions in weights_history/.",
        "# Versioned: score_run stores weights_version; tuning bumps this file.",
        f"version: {version}", "weights:",
    ]
    for name, value in weights.items():
        lines.append(f"  {name}: {value}")
    lines.append("tier_thresholds:")
    for tier, spec in thresholds.items():
        lines.append(f"  {tier}:")
        for key, value in spec.items():
            lines.append(f"    {key}: {value}")
    (CONFIG_DIR / "weights.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
