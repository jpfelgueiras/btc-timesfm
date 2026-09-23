"""Deterministic paired bootstrap tests for forecast model comparisons."""

from __future__ import annotations

import math
from typing import Any, Literal, Sequence

import numpy as np


DEFAULT_BOOTSTRAP_ITERATIONS = 5000
DEFAULT_CONFIDENCE = 0.95
DEFAULT_MIN_PAIRED_SAMPLES = 32
DEFAULT_SEED = 0


def _as_finite_array(values: Sequence[float], name: str) -> np.ndarray:
    array = np.asarray(values, dtype=np.float64)
    if array.ndim != 1:
        raise ValueError(f"{name} must be one-dimensional")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} must contain only finite values")
    return array


def paired_bootstrap_comparison(
    candidate: Sequence[float],
    baseline: Sequence[float],
    *,
    metric: str,
    lower_is_better: bool,
    confidence: float = DEFAULT_CONFIDENCE,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    min_samples: int = DEFAULT_MIN_PAIRED_SAMPLES,
    seed: int = DEFAULT_SEED,
    method: Literal["iid", "moving_block", "stationary"] = "iid",
    block_length: int | None = None,
) -> dict[str, Any]:
    """Compare paired measurements with a deterministic bootstrap confidence interval.

    The returned ``improvement`` is always oriented so positive values favor the
    candidate. For error metrics this is ``baseline - candidate``; for metrics
    where larger is better it is ``candidate - baseline``.
    """
    candidate_array = _as_finite_array(candidate, "candidate")
    baseline_array = _as_finite_array(baseline, "baseline")
    if len(candidate_array) != len(baseline_array):
        raise ValueError("paired comparisons require identical sample counts")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be between 0 and 1")
    if iterations < 100:
        raise ValueError("iterations must be at least 100")
    if min_samples < 1:
        raise ValueError("min_samples must be positive")
    if method not in ("iid", "moving_block", "stationary"):
        raise ValueError("unsupported bootstrap method")
    if block_length is not None and block_length < 1:
        raise ValueError("block_length must be positive")

    samples = len(candidate_array)
    if samples == 0:
        return {
            "metric": metric,
            "samples": 0,
            "candidate_mean": None,
            "baseline_mean": None,
            "candidate_minus_baseline": None,
            "mean_improvement": None,
            "relative_effect_size": None,
            "paired_standardized_effect": None,
            "confidence": confidence,
            "bootstrap_method": method,
            "block_length": block_length,
            "effective_samples": 0.0,
            "improvement_ci": {"lower": None, "upper": None},
            "probability_candidate_better": None,
            "conclusion": "inconclusive",
            "reason": "no_paired_samples",
        }

    raw_delta = candidate_array - baseline_array
    improvement = -raw_delta if lower_is_better else raw_delta
    candidate_mean = float(np.mean(candidate_array))
    baseline_mean = float(np.mean(baseline_array))
    mean_improvement = float(np.mean(improvement))

    rng = np.random.default_rng(seed)
    bootstrap_means: np.ndarray = np.empty(iterations, dtype=np.float64)
    selected_block_length = min(samples, block_length or max(1, round(samples ** (1 / 3))))
    for iteration in range(iterations):
        if method == "iid":
            indices = rng.integers(0, samples, size=samples)
        elif method == "moving_block":
            starts = rng.integers(0, samples, size=math.ceil(samples / selected_block_length))
            indices = np.concatenate(
                [(start + np.arange(selected_block_length)) % samples for start in starts]
            )[:samples]
        else:
            indices = np.empty(samples, dtype=int)
            indices[0] = rng.integers(0, samples)
            restart_probability = 1.0 / selected_block_length
            for position in range(1, samples):
                indices[position] = (
                    rng.integers(0, samples)
                    if rng.random() < restart_probability
                    else (indices[position - 1] + 1) % samples
                )
        bootstrap_means[iteration] = np.mean(improvement[indices])

    alpha = (1.0 - confidence) / 2.0
    lower, upper = np.quantile(bootstrap_means, [alpha, 1.0 - alpha])
    probability_better = float(np.mean(bootstrap_means > 0.0))

    relative_effect = mean_improvement / abs(baseline_mean) if abs(baseline_mean) > 1e-12 else None
    standardized_effect: float | None = None
    if samples > 1:
        paired_std = float(np.std(improvement, ddof=1))
        if paired_std > 1e-12:
            standardized_effect = mean_improvement / paired_std

    if samples < min_samples:
        conclusion = "inconclusive"
        reason = "insufficient_samples"
    elif lower > 0.0:
        conclusion = "candidate_better"
        reason = "confidence_interval_above_zero"
    elif upper < 0.0:
        conclusion = "baseline_better"
        reason = "confidence_interval_below_zero"
    else:
        conclusion = "inconclusive"
        reason = "confidence_interval_crosses_zero"

    return {
        "metric": metric,
        "samples": samples,
        "candidate_mean": round(candidate_mean, 8),
        "baseline_mean": round(baseline_mean, 8),
        "candidate_minus_baseline": round(candidate_mean - baseline_mean, 8),
        "mean_improvement": round(mean_improvement, 8),
        "relative_effect_size": round(relative_effect, 8) if relative_effect is not None else None,
        "paired_standardized_effect": (
            round(standardized_effect, 8) if standardized_effect is not None else None
        ),
        "confidence": confidence,
        "bootstrap_method": method,
        "block_length": selected_block_length,
        "effective_samples": round(samples / selected_block_length, 6),
        "improvement_ci": {
            "lower": round(float(lower), 8),
            "upper": round(float(upper), 8),
        },
        "probability_candidate_better": round(probability_better, 6),
        "conclusion": conclusion,
        "reason": reason,
    }
