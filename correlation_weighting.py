"""Correlation-aware overlay for adaptive BTC ensemble weights.

The base adaptive policy scores each model independently. This module adds a
bounded diversification penalty based only on paired, matured residuals that
were available at the forecast origin. Sparse estimates shrink back to the
base policy, and the existing weight floor/cap remain authoritative.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import numpy as np

from adaptive_weighting import (
    ADAPTIVE_MAX_WEIGHT,
    ADAPTIVE_MIN_WEIGHT,
    _bounded_normalize,
    adaptive_model_weights as base_adaptive_model_weights,
)

DEFAULT_CORRELATION_HISTORY_LIMIT = int(os.getenv("BTC_CORRELATION_HISTORY_LIMIT", "120"))
CORRELATION_MIN_SAMPLES = int(os.getenv("BTC_CORRELATION_MIN_SAMPLES", "12"))
CORRELATION_FULL_SAMPLES = int(os.getenv("BTC_CORRELATION_FULL_SAMPLES", "36"))
CORRELATION_STRENGTH = float(os.getenv("BTC_CORRELATION_PENALTY_STRENGTH", "0.55"))
MAX_CORRELATION_BLEND = float(os.getenv("BTC_CORRELATION_MAX_BLEND", "0.70"))


def _origin(snapshot: dict[str, Any]) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(snapshot["latest_close_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _actual(
    snapshot: dict[str, Any],
    horizon: str,
    model_name: str,
    hour: int,
    actual_by_timestamp: dict[int, float],
) -> float | None:
    try:
        value = float(snapshot["_outcomes"][horizon][model_name]["actual_target_price_usd"])
        if value > 0:
            return value
    except (KeyError, TypeError, ValueError):
        pass
    origin = _origin(snapshot)
    if origin is None:
        return None
    fallback = actual_by_timestamp.get(int(origin.timestamp()) + hour * 3600)
    if fallback is None:
        return None
    actual = float(fallback)
    return actual if actual > 0 else None


def residual_history(
    history: list[dict[str, Any]],
    actual_by_timestamp: dict[int, float],
    model_names: list[str],
    hour: int,
    *,
    history_limit: int = DEFAULT_CORRELATION_HISTORY_LIMIT,
) -> dict[str, dict[str, float]]:
    """Map model -> origin -> signed percentage residual for matured rows."""
    horizon = f"{hour}h"
    result: dict[str, dict[str, float]] = {name: {} for name in model_names}
    complete_origins = 0
    for snapshot in reversed(history):
        origin = _origin(snapshot)
        if origin is None:
            continue
        origin_key = origin.isoformat()
        had_sample = False
        for name in model_names:
            try:
                predicted = float(snapshot["model_predictions"][name][horizon]["price_usd"])
            except (KeyError, TypeError, ValueError):
                continue
            actual = _actual(snapshot, horizon, name, hour, actual_by_timestamp)
            if actual is None:
                continue
            result[name][origin_key] = (predicted - actual) / actual
            had_sample = True
        if had_sample:
            complete_origins += 1
        if complete_origins >= history_limit:
            break
    return result


def residual_correlation_matrix(
    residuals: dict[str, dict[str, float]],
    *,
    min_samples: int = CORRELATION_MIN_SAMPLES,
) -> tuple[dict[str, dict[str, float | None]], dict[str, dict[str, int]]]:
    """Compute pairwise residual correlations on aligned matured origins."""
    names = list(residuals)
    correlations: dict[str, dict[str, float | None]] = {name: {} for name in names}
    samples: dict[str, dict[str, int]] = {name: {} for name in names}
    for left in names:
        for right in names:
            common = sorted(set(residuals[left]) & set(residuals[right]))
            samples[left][right] = len(common)
            if left == right:
                correlations[left][right] = 1.0 if common else None
                continue
            if len(common) < min_samples:
                correlations[left][right] = None
                continue
            x = np.asarray([residuals[left][origin] for origin in common], dtype=float)
            y = np.asarray([residuals[right][origin] for origin in common], dtype=float)
            if float(np.std(x)) <= 1e-12 or float(np.std(y)) <= 1e-12:
                correlations[left][right] = None
                continue
            correlations[left][right] = float(np.corrcoef(x, y)[0, 1])
    return correlations, samples


def _progress(sample_count: int) -> float:
    if sample_count < CORRELATION_MIN_SAMPLES:
        return 0.0
    return min(
        1.0,
        max(
            0.0,
            (sample_count - CORRELATION_MIN_SAMPLES)
            / max(1, CORRELATION_FULL_SAMPLES - CORRELATION_MIN_SAMPLES),
        ),
    )


def correlation_penalties(
    base_weights: dict[str, float],
    correlations: dict[str, dict[str, float | None]],
    pair_samples: dict[str, dict[str, int]],
) -> tuple[dict[str, float], dict[str, Any]]:
    """Calculate sample-shrunk redundancy penalties for each active model."""
    penalties: dict[str, float] = {}
    diagnostics: dict[str, Any] = {}
    for name, weight in base_weights.items():
        weighted_redundancy = 0.0
        peer_weight = 0.0
        usable_samples: list[int] = []
        for peer, peer_base_weight in base_weights.items():
            if peer == name or peer_base_weight <= 0:
                continue
            correlation = correlations.get(name, {}).get(peer)
            if correlation is None:
                continue
            positive_correlation = max(0.0, float(correlation))
            weighted_redundancy += peer_base_weight * positive_correlation**2
            peer_weight += peer_base_weight
            usable_samples.append(pair_samples.get(name, {}).get(peer, 0))
        redundancy = weighted_redundancy / peer_weight if peer_weight > 0 else 0.0
        sample_count = min(usable_samples) if usable_samples else 0
        blend = MAX_CORRELATION_BLEND * _progress(sample_count)
        full_penalty = 1.0 / (1.0 + CORRELATION_STRENGTH * redundancy)
        penalty = 1.0 - blend * (1.0 - full_penalty)
        penalties[name] = penalty
        diagnostics[name] = {
            "base_weight": round(weight, 6),
            "redundancy_score": round(redundancy, 6),
            "paired_samples": sample_count,
            "correlation_blend": round(blend, 6),
            "penalty": round(penalty, 6),
        }
    return penalties, diagnostics


def correlation_aware_model_weights(
    model_names: list[str],
    regime: str,
    hour: int,
    history: list[dict[str, Any]],
    actual_by_timestamp: dict[int, float],
    enabled: bool = True,
    history_limit: int | None = None,
    confidence: float = 1.0,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Apply a conservative residual-correlation overlay to adaptive weights."""
    base_weights, base_diagnostics = base_adaptive_model_weights(
        model_names,
        regime,
        hour,
        history,
        actual_by_timestamp,
        enabled=enabled,
        history_limit=history_limit,
        confidence=confidence,
    )
    residuals = residual_history(
        history,
        actual_by_timestamp,
        model_names,
        hour,
        history_limit=max(
            CORRELATION_MIN_SAMPLES,
            int(history_limit or DEFAULT_CORRELATION_HISTORY_LIMIT),
        ),
    )
    correlations, pair_samples = residual_correlation_matrix(residuals)
    penalties, model_diagnostics = correlation_penalties(base_weights, correlations, pair_samples)
    usable_pairs = [
        pair_samples[left][right]
        for left in model_names
        for right in model_names
        if left < right and correlations[left][right] is not None
    ]

    if not enabled or base_diagnostics.get("mode") != "adaptive" or not usable_pairs:
        diagnostics = {
            **base_diagnostics,
            "correlation_mode": "base_policy",
            "correlation_reason": (
                "disabled"
                if not enabled
                else "base_policy_not_adaptive"
                if base_diagnostics.get("mode") != "adaptive"
                else "insufficient_paired_history"
            ),
            "correlation_min_samples": CORRELATION_MIN_SAMPLES,
            "correlation_history_limit": int(history_limit or DEFAULT_CORRELATION_HISTORY_LIMIT),
            "residual_correlations": correlations,
            "residual_pair_samples": pair_samples,
            "correlation_models": model_diagnostics,
        }
        return base_weights, diagnostics

    adjusted_raw = {name: base_weights[name] * penalties[name] for name in model_names}
    final = _bounded_normalize(adjusted_raw, ADAPTIVE_MIN_WEIGHT, ADAPTIVE_MAX_WEIGHT)
    diagnostics = {
        **base_diagnostics,
        "mode": "correlation_aware_adaptive",
        "correlation_mode": "active",
        "correlation_min_samples": CORRELATION_MIN_SAMPLES,
        "correlation_history_limit": int(history_limit or DEFAULT_CORRELATION_HISTORY_LIMIT),
        "minimum_usable_pair_samples": min(usable_pairs),
        "residual_correlations": correlations,
        "residual_pair_samples": pair_samples,
        "correlation_models": {
            name: {
                **model_diagnostics[name],
                "final_weight": round(final[name], 6),
                "weight_delta": round(final[name] - base_weights[name], 6),
            }
            for name in model_names
        },
    }
    return final, diagnostics


def compare_weight_policies(
    model_names: list[str],
    regime: str,
    hour: int,
    history: list[dict[str, Any]],
    actual_by_timestamp: dict[int, float],
) -> dict[str, Any]:
    """Machine-readable base-vs-correlation policy comparison for backtests."""
    base, base_diagnostics = base_adaptive_model_weights(
        model_names, regime, hour, history, actual_by_timestamp
    )
    diversified, diversified_diagnostics = correlation_aware_model_weights(
        model_names, regime, hour, history, actual_by_timestamp
    )
    return {
        "horizon": f"{hour}h",
        "base_mode": base_diagnostics.get("mode"),
        "correlation_mode": diversified_diagnostics.get("correlation_mode"),
        "base_weights": base,
        "correlation_aware_weights": diversified,
        "weight_delta": {name: diversified[name] - base[name] for name in model_names},
        "diagnostics": diversified_diagnostics,
    }
