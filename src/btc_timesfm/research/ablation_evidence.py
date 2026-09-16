"""Horizon-level evidence and recommendation-only decisions for feature ablations."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Sequence

import numpy as np

from btc_timesfm.forecasting.statistical_significance import paired_bootstrap_comparison

MIN_PROMOTION_SAMPLES = 32
MIN_STABLE_FOLDS = 3


def ablation_manifest(feature_family: str, horizons: Sequence[int]) -> dict[str, Any]:
    """Capture the fixed evaluation policy required to reproduce a family result."""
    policy = {
        "feature_family": feature_family,
        "horizons_hours": list(horizons),
        "minimum_paired_samples": MIN_PROMOTION_SAMPLES,
        "minimum_stable_folds": MIN_STABLE_FOLDS,
        "bootstrap_iterations": 2000,
        "promotion_mode": "recommendation_only",
        "uses_future_information": False,
    }
    digest = hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {**policy, "policy_sha256": digest}


def evaluate_horizon_evidence(
    candidate_errors: Sequence[float],
    baseline_errors: Sequence[float],
    *,
    metric: str,
    seed: int,
    iterations: int = 2000,
) -> dict[str, Any]:
    """Return deterministic effect, CI, sample, and temporal-fold evidence."""
    significance = paired_bootstrap_comparison(
        candidate_errors,
        baseline_errors,
        metric=metric,
        lower_is_better=True,
        min_samples=MIN_PROMOTION_SAMPLES,
        iterations=iterations,
        seed=seed,
    )
    improvements = np.asarray(baseline_errors, dtype=float) - np.asarray(
        candidate_errors, dtype=float
    )
    folds = [fold for fold in np.array_split(improvements, MIN_STABLE_FOLDS) if len(fold)]
    improving_folds = sum(float(np.mean(fold)) > 0.0 for fold in folds)
    stable = len(folds) >= MIN_STABLE_FOLDS and improving_folds == len(folds)
    if significance["conclusion"] == "candidate_better" and stable:
        decision = "recommend_review"
    elif significance["conclusion"] == "baseline_better":
        decision = "do_not_promote_harmful"
    elif int(significance["samples"]) < MIN_PROMOTION_SAMPLES:
        decision = "insufficient_evidence"
    else:
        decision = "do_not_promote_inconclusive"
    return {
        "effect_size": {
            "mean_improvement": significance["mean_improvement"],
            "relative_effect_size": significance["relative_effect_size"],
            "paired_standardized_effect": significance["paired_standardized_effect"],
        },
        "confidence_interval": significance["improvement_ci"],
        "sample_count": significance["samples"],
        "fold_stability": {
            "fold_count": len(folds),
            "improving_folds": improving_folds,
            "all_folds_improve": stable,
        },
        "decision": decision,
        "significance": significance,
    }
