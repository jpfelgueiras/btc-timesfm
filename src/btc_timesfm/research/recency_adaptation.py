#!/usr/bin/env python3
"""Leakage-free evaluation of bounded online and recency-aware adaptation.

Issue #130: the ensemble should adapt faster to structural market change without
overreacting to short-term noise. This module replays frozen per-model forecasts
chronologically through purged walk-forward folds where every policy may only
consume target outcomes whose candle is already visible at the forecast origin.

Techniques scored against the current production adaptive ensemble and
persistence include exponential and linear decay schedules, rolling-window
selection, and uniform (production-style) weighting. Drift and normal periods
are evaluated separately using the production drift detector, and guardrails
shrink recency policies toward the production weights when the effective sample
count is small or a weight change would be extreme.

The module is recommendation-only: it produces significance and championship
evidence through the existing policies but never changes production defaults.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from btc_timesfm.forecasting.adaptive_weighting import (
    ADAPTIVE_DIRECTION_REWARD,
    ADAPTIVE_FULL_SAMPLES,
    ADAPTIVE_MAE_LAMBDA,
    ADAPTIVE_MIN_SAMPLES,
    adaptive_model_weights,
)
from btc_timesfm.forecasting.cross_validation import (
    DEFAULT_EMBARGO_HOURS,
    DEFAULT_MIN_TRAIN_SAMPLES,
    DEFAULT_PURGE_HOURS,
    DEFAULT_ROLLING_TRAIN_SAMPLES,
    assert_no_fold_leakage,
    build_purged_walk_forward_folds,
    fold_definition,
)
from btc_timesfm.forecasting.forecast_engine import TARGET_HOURS, static_model_weights
from btc_timesfm.forecasting.statistical_significance import (
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_CONFIDENCE,
    DEFAULT_MIN_PAIRED_SAMPLES,
    paired_bootstrap_comparison,
)
from btc_timesfm.ops.drift_detection import (
    DriftConfig,
    adaptive_confidence_for_severity,
    evaluate_drift,
)
from btc_timesfm.ops.promotion_policy import evaluate_promotion
from btc_timesfm.research import champion_challenger as challenger_report
from btc_timesfm.research.experiment_registry import ExperimentRegistry

REPORT_PATH = Path("recency_adaptation_report.json")
REPORT_SCHEMA_VERSION = 1
PERSISTENCE_NAME = "persistence"
PRODUCTION_POLICY_NAME = "production_adaptive"
PRODUCTION_CANDIDATE_NAME = "production"
HORIZONS = tuple(f"{hour}h" for hour in TARGET_HOURS)
TECHNIQUES = ("exponential_decay", "linear_decay", "rolling_window", "uniform")

DEFAULT_HISTORY_LIMIT = 200
DEFAULT_MAX_BLEND = 0.55
DEFAULT_MAX_WEIGHT_SHIFT = 0.35
TARGET_INTERVAL_COVERAGE = 0.80
MIN_COVERAGE_SAMPLES = 3
DRIFT_SEGMENT = "drift"
NORMAL_SEGMENT = "normal"


def _mean(values: Sequence[float]) -> float | None:
    items = list(values)
    return float(np.mean(items)) if items else None


def _origin_timestamp(snapshot: Mapping[str, Any]) -> int | None:
    try:
        value = datetime.fromisoformat(str(snapshot["latest_close_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.astimezone(timezone.utc).timestamp())


def _iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


@dataclass(frozen=True)
class RecencyConfig:
    """One bounded online-adaptation policy.

    ``technique`` selects how historical outcomes are weighted per model when
    computing a recency-adjusted performance score:

    - ``exponential_decay``: weight recent outcomes by ``2 ** (-age / half_life)``
    - ``linear_decay``: linearly down-weight outcomes older than ``half_life``
    - ``rolling_window``: uniform weights over the last ``history_limit`` outcomes
    - ``uniform``: uniform weights over all permitted outcomes (production style)

    ``max_blend`` caps how far learned weights can move from the production
    prior, and ``max_weight_shift`` caps the per-model absolute weight change on
    any single origin. Small effective sample sizes scale the blend toward zero,
    and ``min_effective_samples`` is the minimum evidence before adapting at all.
    """

    name: str
    technique: str = "exponential_decay"
    history_limit: int | None = None
    half_life_samples: int = 6
    min_effective_samples: int = ADAPTIVE_MIN_SAMPLES
    full_effective_samples: int = ADAPTIVE_FULL_SAMPLES
    max_blend: float = DEFAULT_MAX_BLEND
    max_weight_shift: float = DEFAULT_MAX_WEIGHT_SHIFT


def default_policies() -> tuple[RecencyConfig, ...]:
    """The catalog of recency/rolling/decay policies evaluated by default."""
    return (
        RecencyConfig(
            name="rolling_24",
            technique="rolling_window",
            history_limit=24,
            max_blend=0.40,
            max_weight_shift=0.25,
        ),
        RecencyConfig(
            name="rolling_48",
            technique="rolling_window",
            history_limit=48,
            max_blend=0.45,
            max_weight_shift=0.30,
        ),
        RecencyConfig(
            name="recency_exp_6",
            technique="exponential_decay",
            half_life_samples=6,
            max_blend=0.50,
            max_weight_shift=0.30,
        ),
        RecencyConfig(
            name="recency_exp_12",
            technique="exponential_decay",
            half_life_samples=12,
            max_blend=0.55,
            max_weight_shift=0.35,
        ),
        RecencyConfig(
            name="recency_linear_12",
            technique="linear_decay",
            half_life_samples=12,
            max_blend=0.45,
            max_weight_shift=0.30,
        ),
    )


def recency_sample_weights(count: int, config: RecencyConfig) -> np.ndarray:
    """Return chronological recency weights, newest sample last."""
    if count <= 0:
        return np.asarray([], dtype=np.float64)
    positions: np.ndarray = np.arange(count, dtype=np.float64)
    age: np.ndarray = (count - 1) - positions
    if config.technique == "exponential_decay":
        half_life = max(1, int(config.half_life_samples))
        return np.exp(-math.log(2.0) * age / half_life)
    if config.technique == "linear_decay":
        half_life = max(1, int(config.half_life_samples))
        return np.clip(1.0 - age / half_life, 0.0, 1.0)
    if config.technique == "rolling_window" and config.history_limit:
        weights: np.ndarray = np.zeros(count, dtype=np.float64)
        weights[max(0, count - int(config.history_limit)) :] = 1.0
        return weights
    return np.ones(count, dtype=np.float64)


def _window_limit(config: RecencyConfig) -> int | None:
    """Bound how many newest eligible samples a policy may consume."""
    if config.technique == "rolling_window" and config.history_limit:
        return max(1, int(config.history_limit))
    if config.technique in {"exponential_decay", "linear_decay"} and config.half_life_samples:
        return max(
            max(1, int(config.half_life_samples)),
            config.min_effective_samples,
            config.full_effective_samples,
        )
    return None


def effective_sample_size(weights: Sequence[float] | np.ndarray) -> float:
    """Kish effective sample size for a recency weight vector."""
    array = np.asarray(weights, dtype=np.float64)
    total = float(np.sum(array))
    if total <= 0:
        return 0.0
    return float(total * total / np.sum(array * array))


def _score_history_for_model(
    history: Sequence[Mapping[str, Any]],
    actual_by_timestamp: Mapping[int, float],
    model_name: str,
    hour: int,
    regime: str | None,
    *,
    current_timestamp: int,
    limit: int | None,
) -> list[dict[str, Any]]:
    """Return matured chronological score rows for one model/horizon.

    Only samples whose target candle matured at-or-before ``current_timestamp``
    and is observable in ``actual_by_timestamp`` participate, which preserves the
    no-lookahead contract. Newest samples win; the limit applies after regime
    filtering so a rare regime can use up to ``limit`` relevant observations.
    """
    horizon = f"{hour}h"
    scores: list[dict[str, Any]] = []
    for snapshot in reversed(history):
        if limit is not None and len(scores) >= limit:
            break
        if regime is not None and snapshot.get("regime") != regime:
            continue
        origin = _origin_timestamp(snapshot)
        if origin is None:
            continue
        target = origin + hour * 3600
        if target > current_timestamp:
            continue
        try:
            actual = float(actual_by_timestamp[target])
            previous_close = float(snapshot["latest_close_usd"])
            item = snapshot["model_predictions"][model_name][horizon]
            predicted = float(item["price_usd"])
        except (KeyError, TypeError, ValueError):
            continue
        if actual <= 0:
            continue
        error = predicted - actual
        q10 = item.get("q10_usd") if isinstance(item, Mapping) else None
        q90 = item.get("q90_usd") if isinstance(item, Mapping) else None
        within: bool | None = None
        if q10 is not None and q90 is not None:
            try:
                within = float(q10) <= actual <= float(q90)
            except (TypeError, ValueError):
                within = None
        predicted_change = predicted - previous_close
        actual_change = actual - previous_close
        scores.append(
            {
                "absolute_error_pct": abs(error) / actual * 100.0,
                "signed_error_pct": error / actual * 100.0,
                "direction_correct": float(
                    (predicted_change > 0 and actual_change > 0)
                    or (predicted_change < 0 and actual_change < 0)
                    or (predicted_change == 0 and actual_change == 0)
                ),
                "within_q10_q90": within,
            }
        )
    scores.reverse()
    return scores


def _weighted_metrics(scores: Sequence[Mapping[str, Any]], weights: np.ndarray) -> dict[str, Any]:
    total = float(np.sum(weights))
    if not scores or total <= 0:
        return {
            "samples": len(scores),
            "effective_samples": 0.0,
            "interval_samples": 0,
            "mae_pct": None,
            "mean_signed_error_pct": None,
            "direction_accuracy": None,
            "interval_coverage": None,
        }
    mae = float(
        np.sum(weights * np.asarray([float(s["absolute_error_pct"]) for s in scores])) / total
    )
    signed = float(
        np.sum(weights * np.asarray([float(s["signed_error_pct"]) for s in scores])) / total
    )
    direction = float(
        np.sum(weights * np.asarray([float(s["direction_correct"]) for s in scores])) / total
    )
    covered = [bool(s["within_q10_q90"]) for s in scores if s.get("within_q10_q90") is not None]
    coverage = float(np.mean(covered)) if covered else None
    return {
        "samples": len(scores),
        "effective_samples": round(effective_sample_size(weights), 4),
        "interval_samples": len(covered),
        "mae_pct": round(mae, 6),
        "mean_signed_error_pct": round(signed, 6),
        "direction_accuracy": round(direction, 6),
        "interval_coverage": round(coverage, 6) if coverage is not None else None,
    }


def _raw_score(metrics: Mapping[str, Any]) -> float:
    """Production-compatible performance score from weighted metrics."""
    mae = metrics.get("mae_pct")
    if mae is None:
        return 1e-9
    direction_accuracy = float(metrics.get("direction_accuracy", 0.5))
    bias = abs(float(metrics.get("mean_signed_error_pct", 0.0)))
    score = math.exp(-ADAPTIVE_MAE_LAMBDA * float(mae))
    score *= 1.0 + ADAPTIVE_DIRECTION_REWARD * (direction_accuracy - 0.5) * 2.0
    score *= math.exp(-0.35 * bias)
    coverage = metrics.get("interval_coverage")
    if coverage is not None and int(metrics.get("interval_samples") or 0) >= MIN_COVERAGE_SAMPLES:
        score *= math.exp(-0.35 * abs(float(coverage) - TARGET_INTERVAL_COVERAGE))
    return max(score, 1e-9)


def _effective_memory_age(model_weights: Sequence[tuple[np.ndarray, int]]) -> float:
    """Weighted mean sample age (0 = newest) across a policy's scored samples."""
    total = 0.0
    weighted_age = 0.0
    for weights, count in model_weights:
        weight_sum = float(np.sum(weights))
        if weight_sum <= 0:
            continue
        positions: np.ndarray = np.arange(count, dtype=np.float64)
        age: np.ndarray = (count - 1) - positions
        total += weight_sum
        weighted_age += float(np.sum(weights * age))
    return weighted_age / total if total > 0 else 0.0


def _cap_weight_shift(
    blended: Mapping[str, float],
    production: Mapping[str, float],
    max_weight_shift: float,
) -> tuple[dict[str, float], bool]:
    """Shrink a weight change toward production weights when it is extreme.

    Because both vectors sum to one, scaling every delta equally preserves the
    unit sum and the per-model bounds without renormalization.
    """
    if max_weight_shift <= 0:
        return dict(production), True
    max_delta = max((abs(blended[name] - production[name]) for name in blended), default=0.0)
    applied = max_delta > max_weight_shift
    scale = max_weight_shift / max_delta if applied else 1.0
    return {
        name: production[name] + scale * (blended[name] - production[name]) for name in blended
    }, applied


def recency_model_weights(
    model_names: list[str],
    regime: str,
    hour: int,
    history: Sequence[Mapping[str, Any]],
    actual_by_timestamp: Mapping[int, float],
    *,
    current_timestamp: int,
    config: RecencyConfig,
    production_weights: Mapping[str, float] | None = None,
    confidence: float = 1.0,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Compute bounded recency-aware weights anchored to the production policy.

    Only outcomes matured at-or-before ``current_timestamp`` participate. The
    learned weights move from the production weights toward a recency-weighted
    performance optimum, scaled by the ``max_blend`` cap and the effective sample
    size (Kish), then clipped by ``max_weight_shift``. With no usable history the
    result is exactly the production anchor (the small-sample guardrail).
    """
    prior = (
        dict(production_weights)
        if production_weights is not None
        else static_model_weights(model_names, regime)
    )
    horizon = f"{hour}h"
    limit = _window_limit(config)
    regime_scores = {
        name: _score_history_for_model(
            history,
            actual_by_timestamp,
            name,
            hour,
            regime,
            current_timestamp=current_timestamp,
            limit=limit,
        )
        for name in model_names
    }
    all_scores = {
        name: _score_history_for_model(
            history,
            actual_by_timestamp,
            name,
            hour,
            None,
            current_timestamp=current_timestamp,
            limit=limit,
        )
        for name in model_names
    }
    min_regime = min((len(scores) for scores in regime_scores.values()), default=0)
    min_all = min((len(scores) for scores in all_scores.values()), default=0)
    if min_regime >= config.min_effective_samples:
        source = "regime"
        selected = regime_scores
    elif min_all >= config.min_effective_samples:
        source = "all_regimes"
        selected = all_scores
    else:
        source = "insufficient_history"
        selected = all_scores

    if source == "insufficient_history":
        return prior, {
            "policy": config.name,
            "technique": config.technique,
            "mode": "static_prior",
            "source": source,
            "horizon": horizon,
            "regime": regime,
            "blend_factor": 0.0,
            "effective_samples": 0.0,
            "effective_memory_age": None,
            "weight_shift_capped": False,
            "max_weight_shift": config.max_weight_shift,
            "guardrail": "insufficient_history",
            "models": {
                name: {
                    "final_weight": round(prior[name], 6),
                    "production_weight": round(prior[name], 6),
                }
                for name in model_names
            },
        }

    model_weight_rows: list[tuple[np.ndarray, int]] = []
    raw_scores: dict[str, float] = {}
    metrics: dict[str, dict[str, Any]] = {}
    for name in model_names:
        scores = selected[name]
        weights = recency_sample_weights(len(scores), config)
        weighted = _weighted_metrics(scores, weights)
        metrics[name] = weighted
        raw_scores[name] = _raw_score(weighted)
        if len(scores):
            model_weight_rows.append((weights, len(scores)))
    raw_total = sum(raw_scores.values())
    adaptive = {name: score / raw_total for name, score in raw_scores.items()}
    effective_samples = min(float(metrics[name]["effective_samples"]) for name in model_names)
    progress = min(
        1.0,
        max(
            0.0,
            (effective_samples - config.min_effective_samples)
            / max(1, config.full_effective_samples - config.min_effective_samples),
        ),
    )
    blend = config.max_blend * progress * max(0.0, min(1.0, float(confidence)))
    blended = {name: (1.0 - blend) * prior[name] + blend * adaptive[name] for name in model_names}
    final, capped = _cap_weight_shift(blended, prior, config.max_weight_shift)
    return final, {
        "policy": config.name,
        "technique": config.technique,
        "mode": "recency_adaptive" if blend > 0 else "production_anchor",
        "source": source,
        "horizon": horizon,
        "regime": regime,
        "blend_factor": round(blend, 6),
        "effective_samples": round(effective_samples, 4),
        "effective_memory_age": round(_effective_memory_age(model_weight_rows), 4),
        "weight_shift_capped": capped,
        "max_weight_shift": config.max_weight_shift,
        "models": {
            name: {
                "weighted_metrics": metrics[name],
                "raw_score": round(raw_scores[name], 8),
                "adaptive_weight": round(adaptive[name], 6),
                "final_weight": round(final[name], 6),
                "production_weight": round(prior[name], 6),
            }
            for name in model_names
        },
    }


def _weighted_ensemble_price(
    current: float,
    model_predictions: Mapping[str, Mapping[str, Mapping[str, float]]],
    horizon: str,
    weights: Mapping[str, float],
) -> float:
    """Mirror production's normalized weighted geometric price ensemble."""
    weighted_log_changes = 0.0
    total_weight = 0.0
    for name, weight in weights.items():
        if weight <= 0 or name not in model_predictions:
            continue
        try:
            price = float(model_predictions[name][horizon]["price_usd"])
        except (KeyError, TypeError, ValueError):
            continue
        weighted_log_changes += weight * math.log(price / current)
        total_weight += weight
    if total_weight <= 0:
        return current
    return current * math.exp(weighted_log_changes / total_weight)


def _score(current: float, predicted: float, actual: float) -> dict[str, float]:
    predicted_change = predicted - current
    actual_change = actual - current
    return {
        "absolute_error_pct": abs(predicted - actual) / actual * 100.0,
        "signed_error_pct": (predicted - actual) / actual * 100.0,
        "direction_correct": float(
            (predicted_change > 0 and actual_change > 0)
            or (predicted_change < 0 and actual_change < 0)
            or (predicted_change == 0 and actual_change == 0)
        ),
    }


def _aggregate(scores: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not scores:
        return {
            "samples": 0,
            "mae_pct": None,
            "mean_signed_error_pct": None,
            "direction_accuracy": None,
        }
    return {
        "samples": len(scores),
        "mae_pct": round(float(np.mean([float(s["absolute_error_pct"]) for s in scores])), 6),
        "mean_signed_error_pct": round(
            float(np.mean([float(s["signed_error_pct"]) for s in scores])), 6
        ),
        "direction_accuracy": round(
            float(np.mean([bool(s["direction_correct"]) for s in scores])), 6
        ),
    }


def _snapshot(sample: Mapping[str, Any]) -> dict[str, Any]:
    forecast = sample["forecast"]
    return {
        "latest_close_at": forecast["latest_close_at"],
        "latest_close_usd": forecast["latest_close_usd"],
        "regime": forecast["regime"],
        "market_features": dict(forecast.get("market_features") or {}),
        "model_predictions": forecast["model_predictions"],
        "predictions": forecast.get("predictions", {}),
        "model_weights": forecast.get("model_weights", {}),
    }


def _persistence_price(
    current: float,
    model_predictions: Mapping[str, Any],
    horizon: str,
) -> float:
    try:
        return float(model_predictions[PERSISTENCE_NAME][horizon]["price_usd"])
    except (KeyError, TypeError, ValueError):
        return current


def drift_state_at_origin(
    history: Sequence[Mapping[str, Any]],
    actual_by_timestamp: Mapping[int, float],
    current_timestamp: int,
    current_sample: Mapping[str, Any],
    drift_config: DriftConfig,
) -> str:
    """Evaluate drift with only outcomes matured at-or-before the current origin."""
    prediction_rows: list[dict[str, Any]] = []
    feature_rows: list[dict[str, Any]] = []
    for snapshot in history:
        origin = _origin_timestamp(snapshot)
        if origin is None:
            continue
        origin_at = str(snapshot.get("latest_close_at"))
        features = snapshot.get("market_features")
        if isinstance(features, Mapping):
            feature_rows.append({"origin_at": origin_at, "market_features": dict(features)})
        predictions = snapshot.get("model_predictions") or {}
        for model_name, horizons in predictions.items():
            if not isinstance(horizons, Mapping):
                continue
            for hour in TARGET_HOURS:
                target = origin + hour * 3600
                if target > current_timestamp:
                    continue
                try:
                    actual = float(actual_by_timestamp[target])
                    item = horizons[f"{hour}h"]
                    predicted = float(item["price_usd"])
                    previous_close = float(snapshot["latest_close_usd"])
                except (KeyError, TypeError, ValueError):
                    continue
                if actual <= 0:
                    continue
                predicted_change = predicted - previous_close
                actual_change = actual - previous_close
                prediction_rows.append(
                    {
                        "origin_at": origin_at,
                        "target_at": _iso(target),
                        "model_name": str(model_name),
                        "horizon_hours": hour,
                        "absolute_error_pct": abs(predicted - actual) / actual * 100.0,
                        "signed_error_pct": (predicted - actual) / actual * 100.0,
                        "direction_correct": float(
                            (predicted_change > 0 and actual_change > 0)
                            or (predicted_change < 0 and actual_change < 0)
                            or (predicted_change == 0 and actual_change == 0)
                        ),
                    }
                )
    current_forecast = current_sample.get("forecast")
    report = evaluate_drift(
        prediction_rows,
        feature_rows,
        current_features=(
            dict(current_forecast.get("market_features") or {})
            if isinstance(current_forecast, Mapping)
            else None
        ),
        current_origin_at=(
            str(current_forecast.get("latest_close_at"))
            if isinstance(current_forecast, Mapping)
            else None
        ),
        config=drift_config,
    )
    return str(report.get("severity", "none"))


def _segment(severity: str) -> str:
    return DRIFT_SEGMENT if severity != "none" else NORMAL_SEGMENT


def _objective_scores(per_origin: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[float]:
    first = next(iter(per_origin.values()))
    return [
        float(
            np.mean(
                [float(per_origin[horizon][index]["absolute_error_pct"]) for horizon in HORIZONS]
            )
        )
        for index in range(len(first))
    ]


def _objective_direction(per_origin: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[float]:
    first = next(iter(per_origin.values()))
    return [
        float(
            np.mean([bool(per_origin[horizon][index]["direction_correct"]) for horizon in HORIZONS])
        )
        for index in range(len(first))
    ]


def _paired_significance(
    candidate: Sequence[float],
    baseline: Sequence[float],
    *,
    metric: str,
    lower_is_better: bool,
    bootstrap_iterations: int,
    min_paired_samples: int,
) -> dict[str, Any]:
    return paired_bootstrap_comparison(
        candidate,
        baseline,
        metric=metric,
        lower_is_better=lower_is_better,
        iterations=bootstrap_iterations,
        min_samples=min_paired_samples,
    )


def evaluate_recency_adaptation(
    samples: list[dict[str, Any]],
    actual_by_timestamp: dict[int, float],
    *,
    policies: Sequence[RecencyConfig] = (),
    history_limit: int = DEFAULT_HISTORY_LIMIT,
    drift_config: DriftConfig | None = None,
    folds: int = 3,
    min_train_samples: int = DEFAULT_MIN_TRAIN_SAMPLES,
    purge_hours: int = DEFAULT_PURGE_HOURS,
    embargo_hours: int = DEFAULT_EMBARGO_HOURS,
    mode: str = "expanding",
    rolling_train_samples: int = DEFAULT_ROLLING_TRAIN_SAMPLES,
    apply_drift_confidence: bool = True,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    min_paired_samples: int = DEFAULT_MIN_PAIRED_SAMPLES,
    run_id: str | None = None,
    git_sha: str | None = None,
) -> dict[str, Any]:
    """Replay adaptation policies on identical frozen origins without leakage.

    Purged walk-forward folds define the permitted history. At every validation
    origin each policy may consume only target outcomes observable at that
    origin, current-regime history is preferred, and drift state is evaluated
    from matured outcomes only. The report compares every policy against the
    production adaptive ensemble and persistence, separates drift and normal
    periods, and records responsiveness/stability, guardrail application, and
    promotion evidence through the existing significance/challenger policies.
    """
    active_policies = tuple(policies) if policies else default_policies()
    active_drift = drift_config or DriftConfig.from_env()
    ordered = sorted(samples, key=lambda item: int(item["origin_timestamp"]))
    if not ordered:
        raise ValueError("recency-adaptation evaluation requires at least one sample")
    origin_timestamps = [int(sample["origin_timestamp"]) for sample in ordered]
    model_names: list[str] = []
    for sample in ordered:
        for name in sample["forecast"]["model_predictions"]:
            if name not in model_names:
                model_names.append(name)
    if not model_names:
        raise ValueError("samples contain no model predictions")

    active_folds = build_purged_walk_forward_folds(
        origin_timestamps,
        folds=folds,
        min_train_samples=min_train_samples,
        purge_hours=purge_hours,
        embargo_hours=embargo_hours,
        mode=mode,
        rolling_train_samples=rolling_train_samples,
        max_target_hours=max(TARGET_HOURS),
    )
    for fold in active_folds:
        assert_no_fold_leakage(fold, origin_timestamps, max_target_hours=max(TARGET_HOURS))

    policy_names = [PRODUCTION_POLICY_NAME, *(policy.name for policy in active_policies)]
    policy_by_name = {
        PRODUCTION_POLICY_NAME: None,
        **{policy.name: policy for policy in active_policies},
    }

    scores: dict[str, dict[str, list[dict[str, Any]]]] = {
        name: {horizon: [] for horizon in HORIZONS} for name in policy_names
    }
    persistence_scores: dict[str, list[dict[str, Any]]] = {horizon: [] for horizon in HORIZONS}
    all_comparison_names = [*policy_names, PERSISTENCE_NAME]
    segment_scores: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {
        segment: {name: {horizon: [] for horizon in HORIZONS} for name in all_comparison_names}
        for segment in (DRIFT_SEGMENT, NORMAL_SEGMENT)
    }
    fold_scores: dict[str, dict[str, dict[int, list[dict[str, Any]]]]] = {
        name: {horizon: {fold.fold: [] for fold in active_folds} for horizon in HORIZONS}
        for name in all_comparison_names
    }
    weight_turnover: dict[str, dict[str, list[float]]] = {
        name: {horizon: [] for horizon in HORIZONS} for name in policy_names
    }
    memory_trajectory: dict[str, dict[str, list[float]]] = {
        name: {horizon: [] for horizon in HORIZONS} for name in policy_names
    }
    guardrail_counts: dict[str, dict[str, int]] = {
        name: {
            "insufficient_history": 0,
            "anchored_to_production": 0,
            "weight_shift_capped": 0,
        }
        for name in policy_names
    }
    origin_records: list[dict[str, Any]] = []
    last_weights: dict[str, dict[str, tuple[float, ...]]] = {}

    for fold in active_folds:
        history: list[dict[str, Any]] = [_snapshot(ordered[index]) for index in fold.train_indices]
        history = history[-history_limit:]
        for index in fold.validation_indices:
            sample = ordered[index]
            timestamp = int(sample["origin_timestamp"])
            forecast = sample["forecast"]
            current = float(sample["current_price"])
            regime = str(forecast["regime"])
            model_predictions = forecast["model_predictions"]
            visible_actuals = {
                target: value
                for target, value in actual_by_timestamp.items()
                if target <= timestamp
            }
            severity = drift_state_at_origin(
                history, actual_by_timestamp, timestamp, sample, active_drift
            )
            confidence = adaptive_confidence_for_severity(severity, active_drift)
            if not apply_drift_confidence:
                confidence = 1.0
            segment = _segment(severity)

            for hour in TARGET_HOURS:
                horizon = f"{hour}h"
                actual = float(sample["actuals"][horizon])
                production_weights, production_diagnostics = adaptive_model_weights(
                    model_names,
                    regime,
                    hour,
                    history,
                    visible_actuals,
                    history_limit=history_limit,
                    confidence=confidence,
                )
                weights_by_policy: dict[str, dict[str, float]] = {
                    PRODUCTION_POLICY_NAME: production_weights
                }
                diagnostics_by_policy: dict[str, dict[str, Any]] = {}
                for policy in active_policies:
                    weights, diagnostics = recency_model_weights(
                        model_names,
                        regime,
                        hour,
                        history,
                        visible_actuals,
                        current_timestamp=timestamp,
                        config=policy,
                        production_weights=production_weights,
                        confidence=confidence,
                    )
                    weights_by_policy[policy.name] = weights
                    diagnostics_by_policy[policy.name] = diagnostics

                for name, weights in weights_by_policy.items():
                    predicted = _weighted_ensemble_price(
                        current, model_predictions, horizon, weights
                    )
                    scored = _score(current, predicted, actual)
                    scores[name][horizon].append(scored)
                    segment_scores[segment][name][horizon].append(scored)
                    fold_scores[name][horizon][fold.fold].append(scored)
                    weight_tuple = tuple(float(weights.get(model, 0.0)) for model in model_names)
                    previous = last_weights.get(name, {}).get(horizon)
                    if previous is not None:
                        weight_turnover[name][horizon].append(
                            sum(abs(a - b) for a, b in zip(previous, weight_tuple, strict=True))
                        )
                    diagnostics = diagnostics_by_policy.get(name) or {}
                    memory: float | None = None
                    if name == PRODUCTION_POLICY_NAME:
                        sample_count = int(diagnostics.get("sample_count") or 0)
                        if sample_count > 0:
                            memory = max(0.0, (sample_count - 1) / 2.0)
                    else:
                        raw_memory = diagnostics.get("effective_memory_age")
                        if raw_memory is not None:
                            memory = float(raw_memory)
                        if diagnostics.get("mode") == "static_prior":
                            guardrail_counts[name]["insufficient_history"] += 1
                            guardrail_counts[name]["anchored_to_production"] += 1
                        if diagnostics.get("weight_shift_capped"):
                            guardrail_counts[name]["weight_shift_capped"] += 1
                    if memory is not None:
                        memory_trajectory[name][horizon].append(memory)
                    last_weights.setdefault(name, {})[horizon] = weight_tuple

                persistence_price = _persistence_price(current, model_predictions, horizon)
                persistence_scored = _score(current, persistence_price, actual)
                persistence_scores[horizon].append(persistence_scored)
                segment_scores[segment][PERSISTENCE_NAME][horizon].append(persistence_scored)
                fold_scores[PERSISTENCE_NAME][horizon][fold.fold].append(persistence_scored)

            origin_records.append(
                {
                    "origin_at": sample.get("origin_at") or forecast.get("latest_close_at"),
                    "origin_timestamp": timestamp,
                    "regime": regime,
                    "severity": severity,
                    "segment": segment,
                    "fold": fold.fold,
                }
            )
            history.append(_snapshot(sample))
            history = history[-history_limit:]

    current_severity = str(origin_records[-1]["severity"]) if origin_records else "none"
    fold_definitions = [fold_definition(fold, origin_timestamps) for fold in active_folds]

    by_horizon = _by_horizon_metrics(scores, persistence_scores)
    by_segment = _by_segment_metrics(segment_scores)
    responsiveness_stability = _responsiveness_stability(
        weight_turnover=weight_turnover,
        memory_trajectory=memory_trajectory,
        policy_names=policy_names,
    )
    guardrails = _guardrail_report(policy_names, guardrail_counts, active_policies)
    significance = _significance_report(
        scores=scores,
        persistence_scores=persistence_scores,
        segment_scores=segment_scores,
        bootstrap_iterations=bootstrap_iterations,
        min_paired_samples=min_paired_samples,
    )
    promotion = _promotion_evidence(
        origin_records=origin_records,
        scores=scores,
        persistence_scores=persistence_scores,
        fold_scores=fold_scores,
        policies=active_policies,
        severity=current_severity,
        bootstrap_iterations=bootstrap_iterations,
        min_paired_samples=min_paired_samples,
    )

    leakage_safety: dict[str, Any] = {
        "origin_time_only_actuals": True,
        "purged_walk_forward": True,
        "mode": mode,
        "purge_hours": purge_hours,
        "embargo_hours": embargo_hours,
        "min_train_samples": min_train_samples,
        "max_target_hours": max(TARGET_HOURS),
        "fold_definitions": fold_definitions,
        "assert_fold_leakage": True,
    }
    drift_segmentation: dict[str, Any] = {
        "method": "btc_timesfm.ops.drift_detection.evaluate_drift",
        "config": asdict(active_drift),
        "apply_drift_confidence": apply_drift_confidence,
        "origins": {
            "total": len(origin_records),
            "drift": sum(1 for record in origin_records if record["segment"] == DRIFT_SEGMENT),
            "normal": sum(1 for record in origin_records if record["segment"] == NORMAL_SEGMENT),
        },
        "current_severity": current_severity,
    }
    policy_configs: dict[str, Any] = {}
    for name in policy_names:
        candidate_policy = policy_by_name[name]
        policy_configs[name] = asdict(candidate_policy) if candidate_policy is not None else {}
    data_identity = {
        "source": "frozen_forecast_samples",
        "samples": len(ordered),
        "first_origin_at": _iso(origin_timestamps[0]),
        "last_origin_at": _iso(origin_timestamps[-1]),
        "actuals_count": len(actual_by_timestamp),
        "actuals_sha256": hashlib.sha256(
            json.dumps(
                sorted(
                    (int(timestamp), float(price))
                    for timestamp, price in actual_by_timestamp.items()
                )
            ).encode("utf-8")
        ).hexdigest(),
    }
    experiment_manifest = build_manifest(
        run_id=run_id,
        run_type="recency_adaptation_evaluation",
        created_at=None,
        model_names=model_names,
        policy_configs=policy_configs,
        leakage=leakage_safety,
        drift_config=asdict(active_drift),
        data_identity=data_identity,
        git_sha=git_sha,
    )

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "evaluation": "recency_adaptation",
        "samples_evaluated": len(origin_records),
        "origin_count": len(origin_records),
        "horizons": list(HORIZONS),
        "policy_configs": policy_configs,
        "leakage_safety": leakage_safety,
        "drift_segmentation": drift_segmentation,
        "by_horizon": by_horizon,
        "by_segment": by_segment,
        "responsiveness_stability": responsiveness_stability,
        "guardrails": guardrails,
        "significance": significance,
        "promotion": promotion,
        "metrics_for_registry": promotion.get("candidate_metrics"),
        "experiment_manifest": experiment_manifest,
    }


def _fold_aggregates(
    fold_scores: Mapping[str, Mapping[str, Mapping[int, Sequence[Mapping[str, Any]]]]],
    policy_name: str,
    fold_ids: Sequence[int],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for fold_id in fold_ids:
        rows = [
            score
            for horizon in HORIZONS
            for score in fold_scores.get(policy_name, {}).get(horizon, {}).get(fold_id, [])
        ]
        aggregate = _aggregate(rows)
        aggregate["fold"] = fold_id
        result.append(aggregate)
    return result


def _by_horizon_metrics(
    scores: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    persistence_scores: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for horizon in HORIZONS:
        production = _aggregate(scores[PRODUCTION_POLICY_NAME][horizon])
        persistence = _aggregate(persistence_scores[horizon])
        policies = {
            name: _aggregate(scores[name][horizon])
            for name in scores
            if name != PRODUCTION_POLICY_NAME
        }
        result[horizon] = {
            "production_adaptive": production,
            "persistence": persistence,
            "production_minus_persistence_mae_pct": (
                round(float(production["mae_pct"]) - float(persistence["mae_pct"]), 6)
                if production["mae_pct"] is not None and persistence["mae_pct"] is not None
                else None
            ),
            "policies": policies,
            "policy_mae_delta_vs_production_pct": {
                name: (
                    round(float(metrics["mae_pct"]) - float(production["mae_pct"]), 6)
                    if metrics["mae_pct"] is not None and production["mae_pct"] is not None
                    else None
                )
                for name, metrics in policies.items()
            },
        }
    return result


def _by_segment_metrics(
    segment_scores: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
) -> dict[str, Any]:
    return {
        segment: {
            horizon: {
                "policies": {
                    name: _aggregate(rows)
                    for name, by_horizon in by_name.items()
                    if name != PERSISTENCE_NAME
                    for rows in [by_horizon[horizon]]
                },
                "persistence": _aggregate(by_name[PERSISTENCE_NAME][horizon]),
            }
            for horizon in HORIZONS
        }
        for segment, by_name in segment_scores.items()
    }


def _responsiveness_stability(
    *,
    weight_turnover: Mapping[str, Mapping[str, Sequence[float]]],
    memory_trajectory: Mapping[str, Mapping[str, Sequence[float]]],
    policy_names: Sequence[str],
) -> dict[str, Any]:
    """Report responsiveness (small effective memory) vs stability (low turnover)."""
    by_policy: dict[str, Any] = {}
    by_horizon: dict[str, Any] = {}
    for name in policy_names:
        responsiveness_values: list[float] = []
        stability_values: list[float] = []
        per_horizon: dict[str, Any] = {}
        for horizon in HORIZONS:
            memory = memory_trajectory.get(name, {}).get(horizon, [])
            movement = weight_turnover.get(name, {}).get(horizon, [])
            turnover = float(np.mean(movement)) if movement else 0.0
            stability = 1.0 / (1.0 + turnover)
            responsiveness = 1.0 / (1.0 + (float(np.mean(memory)) if memory else 0.0))
            per_horizon[horizon] = {
                "weight_turnover_l1": round(turnover, 6),
                "stability_score": round(stability, 6),
                "effective_memory_origins": round(float(np.mean(memory)), 3) if memory else None,
                "responsiveness_score": round(responsiveness, 6),
            }
            responsiveness_values.append(responsiveness)
            stability_values.append(stability)
        by_policy[name] = {
            "responsiveness_score": round(_mean(responsiveness_values) or 0.0, 6),
            "stability_score": round(_mean(stability_values) or 0.0, 6),
            "by_horizon": per_horizon,
        }
    for horizon in HORIZONS:
        by_horizon[horizon] = {
            name: by_policy[name]["by_horizon"][horizon] for name in policy_names
        }
    return {
        "definition": (
            "responsiveness = 1/(1 + effective_recency_memory); "
            "stability = 1/(1 + mean L1 weight turnover between consecutive origins)"
        ),
        "by_policy": by_policy,
        "by_horizon": by_horizon,
    }


def _guardrail_report(
    policy_names: Sequence[str],
    guardrail_counts: Mapping[str, Mapping[str, int]],
    policies: Sequence[RecencyConfig],
) -> dict[str, Any]:
    by_policy: dict[str, Any] = {}
    for name in policy_names:
        if name == PRODUCTION_POLICY_NAME:
            by_policy[name] = {"active": True}
            continue
        config = next((policy for policy in policies if policy.name == name), None)
        by_policy[name] = {
            "active": True,
            "min_effective_samples": config.min_effective_samples if config else None,
            "full_effective_samples": config.full_effective_samples if config else None,
            "max_weight_shift": config.max_weight_shift if config else None,
            "insufficient_history_origins": guardrail_counts[name]["insufficient_history"],
            "anchored_to_production_origins": guardrail_counts[name]["anchored_to_production"],
            "weight_shift_capped_origins": guardrail_counts[name]["weight_shift_capped"],
        }
    return {
        "small_sample_guardrail": (
            "blend scales from 0 to max_blend between min_effective_samples and "
            "full_effective_samples effective samples; insufficient history anchors "
            "exactly to the production weights"
        ),
        "extreme_change_guardrail": (
            "per-model weight change vs production is capped at max_weight_shift per origin"
        ),
        "by_policy": by_policy,
    }


def _significance_report(
    *,
    scores: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    persistence_scores: Mapping[str, Sequence[Mapping[str, Any]]],
    segment_scores: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
    bootstrap_iterations: int,
    min_paired_samples: int,
) -> dict[str, Any]:
    production = scores[PRODUCTION_POLICY_NAME]
    persistence = persistence_scores
    vs_production: dict[str, Any] = {}
    vs_persistence: dict[str, Any] = {}
    for name, by_horizon in scores.items():
        if name == PRODUCTION_POLICY_NAME:
            continue
        vs_production[name] = {
            "objective": _paired_significance(
                _objective_scores(by_horizon),
                _objective_scores(production),
                metric="mae_pct",
                lower_is_better=True,
                bootstrap_iterations=bootstrap_iterations,
                min_paired_samples=min_paired_samples,
            ),
            "by_horizon": {
                horizon: _paired_significance(
                    [float(row["absolute_error_pct"]) for row in by_horizon[horizon]],
                    [float(row["absolute_error_pct"]) for row in production[horizon]],
                    metric=f"{horizon}_mae_pct",
                    lower_is_better=True,
                    bootstrap_iterations=bootstrap_iterations,
                    min_paired_samples=min_paired_samples,
                )
                for horizon in HORIZONS
            },
        }
        vs_persistence[name] = {
            "objective": _paired_significance(
                _objective_scores(by_horizon),
                _objective_scores(persistence),
                metric="mae_pct",
                lower_is_better=True,
                bootstrap_iterations=bootstrap_iterations,
                min_paired_samples=min_paired_samples,
            ),
            "by_horizon": {
                horizon: _paired_significance(
                    [float(row["absolute_error_pct"]) for row in by_horizon[horizon]],
                    [float(row["absolute_error_pct"]) for row in persistence[horizon]],
                    metric=f"{horizon}_mae_pct",
                    lower_is_better=True,
                    bootstrap_iterations=bootstrap_iterations,
                    min_paired_samples=min_paired_samples,
                )
                for horizon in HORIZONS
            },
        }

    by_segment: dict[str, Any] = {}
    for segment, policies_by_horizon in segment_scores.items():
        segment_by_policy: dict[str, Any] = {}
        for name, horizons in policies_by_horizon.items():
            if name in {PRODUCTION_POLICY_NAME, PERSISTENCE_NAME}:
                continue
            segment_by_policy[name] = {
                "vs_production": {
                    "objective": _paired_significance(
                        _objective_scores(horizons),
                        _objective_scores(policies_by_horizon[PRODUCTION_POLICY_NAME]),
                        metric="mae_pct",
                        lower_is_better=True,
                        bootstrap_iterations=bootstrap_iterations,
                        min_paired_samples=min_paired_samples,
                    ),
                },
                "vs_persistence": {
                    "objective": _paired_significance(
                        _objective_scores(horizons),
                        _objective_scores(policies_by_horizon[PERSISTENCE_NAME]),
                        metric="mae_pct",
                        lower_is_better=True,
                        bootstrap_iterations=bootstrap_iterations,
                        min_paired_samples=min_paired_samples,
                    ),
                },
            }
        by_segment[segment] = segment_by_policy

    return {
        "method": "paired_bootstrap",
        "confidence": DEFAULT_CONFIDENCE,
        "iterations": bootstrap_iterations,
        "minimum_paired_samples": min_paired_samples,
        "pairing_key": "forecast_origin",
        "vs_production": vs_production,
        "vs_persistence": vs_persistence,
        "by_segment": by_segment,
    }


def _candidate_block(
    *,
    policy_name: str,
    fold_key: str,
    parameters: Mapping[str, Any],
    origin_records: Sequence[Mapping[str, Any]],
    by_horizon: Mapping[str, Sequence[Mapping[str, Any]]],
    persistence_by_horizon: Mapping[str, Sequence[Mapping[str, Any]]],
    fold_scores: Mapping[str, Mapping[str, Mapping[int, Sequence[Mapping[str, Any]]]]],
) -> dict[str, Any]:
    per_horizon = {horizon: _aggregate(by_horizon[horizon]) for horizon in HORIZONS}
    regimes = sorted({str(record["regime"]) for record in origin_records})
    by_regime: dict[str, Any] = {}
    for regime in regimes:
        indices = [i for i, record in enumerate(origin_records) if str(record["regime"]) == regime]
        if not indices:
            continue
        by_regime[regime] = {
            horizon: _aggregate([by_horizon[horizon][i] for i in indices]) for horizon in HORIZONS
        }
    fold_ids = _fold_ids_for(fold_scores)
    folds = _fold_aggregates(fold_scores, fold_key, fold_ids)
    counts = len(by_horizon[HORIZONS[0]])
    objective_mae = _objective_scores(by_horizon)
    objective_direction = _objective_direction(by_horizon)
    persistence_objective = _objective_scores(persistence_by_horizon)
    return {
        "name": policy_name,
        "parameters": dict(parameters),
        "samples": counts,
        "objective_mae_pct": round(float(np.mean(objective_mae)), 6),
        "mean_direction_accuracy": round(float(np.mean(objective_direction)), 6),
        "by_horizon": per_horizon,
        "by_regime": by_regime,
        "persistence_by_horizon": {
            horizon: _aggregate(persistence_by_horizon[horizon]) for horizon in HORIZONS
        },
        "folds": folds,
        "paired_metrics": {
            "origins": [str(record["origin_at"]) for record in origin_records],
            "mae_pct": [round(value, 8) for value in objective_mae],
            "direction_accuracy": [round(value, 8) for value in objective_direction],
            "by_horizon": {
                horizon: [float(row["absolute_error_pct"]) for row in by_horizon[horizon]]
                for horizon in HORIZONS
            },
            "persistence_mae_pct": [round(value, 8) for value in persistence_objective],
        },
    }


def _fold_ids_for(
    fold_scores: Mapping[str, Mapping[str, Mapping[int, Sequence[Mapping[str, Any]]]]],
) -> list[int]:
    ids: set[int] = set()
    for horizons in fold_scores.values():
        for by_fold in horizons.values():
            ids.update(by_fold)
    return sorted(ids)


def _significance_comparison(
    candidate: Mapping[str, Any],
    production: Mapping[str, Any],
    *,
    bootstrap_iterations: int,
    min_paired_samples: int,
) -> dict[str, Any]:
    candidate_metrics = candidate["paired_metrics"]
    production_metrics = production["paired_metrics"]
    candidate_vs_production = {
        "mae_pct": _paired_significance(
            candidate_metrics["mae_pct"],
            production_metrics["mae_pct"],
            metric="mae_pct",
            lower_is_better=True,
            bootstrap_iterations=bootstrap_iterations,
            min_paired_samples=min_paired_samples,
        ),
        "direction_accuracy": _paired_significance(
            candidate_metrics["direction_accuracy"],
            production_metrics["direction_accuracy"],
            metric="direction_accuracy",
            lower_is_better=False,
            bootstrap_iterations=bootstrap_iterations,
            min_paired_samples=min_paired_samples,
        ),
        "by_horizon_mae_pct": {
            horizon: _paired_significance(
                candidate_metrics["by_horizon"][horizon],
                production_metrics["by_horizon"][horizon],
                metric=f"{horizon}_mae_pct",
                lower_is_better=True,
                bootstrap_iterations=bootstrap_iterations,
                min_paired_samples=min_paired_samples,
            )
            for horizon in HORIZONS
        },
    }
    candidate_vs_persistence = {
        "mae_pct": _paired_significance(
            candidate_metrics["mae_pct"],
            candidate_metrics["persistence_mae_pct"],
            metric="mae_pct",
            lower_is_better=True,
            bootstrap_iterations=bootstrap_iterations,
            min_paired_samples=min_paired_samples,
        )
    }
    return {
        "method": "paired_bootstrap",
        "confidence": DEFAULT_CONFIDENCE,
        "iterations": bootstrap_iterations,
        "minimum_paired_samples": min_paired_samples,
        "pairing_key": "forecast_origin",
        "candidate_vs_production": candidate_vs_production,
        "candidate_vs_persistence": candidate_vs_persistence,
    }


def _promotion_evidence(
    *,
    origin_records: Sequence[Mapping[str, Any]],
    scores: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    persistence_scores: Mapping[str, Sequence[Mapping[str, Any]]],
    fold_scores: Mapping[str, Mapping[str, Mapping[int, Sequence[Mapping[str, Any]]]]],
    policies: Sequence[RecencyConfig],
    severity: str,
    bootstrap_iterations: int,
    min_paired_samples: int,
) -> dict[str, Any]:
    """Build the optimizer/challenger-shaped report the existing policies consume."""
    policy_by_name = {policy.name: policy for policy in policies}
    candidates: list[dict[str, Any]] = []
    for name in scores:
        if name == PRODUCTION_POLICY_NAME:
            candidate_name = PRODUCTION_CANDIDATE_NAME
            parameters: dict[str, Any] = {"policy": "production_adaptive"}
        elif name in policy_by_name:
            candidate_name = name
            parameters = asdict(policy_by_name[name])
        else:
            continue
        candidates.append(
            _candidate_block(
                policy_name=candidate_name,
                fold_key=name,
                parameters=parameters,
                origin_records=origin_records,
                by_horizon=scores[name],
                persistence_by_horizon=persistence_scores,
                fold_scores=fold_scores,
            )
        )
    if not candidates:
        return {"available": False, "reason": "no_policy_candidates"}
    production = next(item for item in candidates if item["name"] == PRODUCTION_CANDIDATE_NAME)
    recency_candidates = [item for item in candidates if item["name"] != PRODUCTION_CANDIDATE_NAME]
    best = min(
        recency_candidates,
        key=lambda item: float(item["objective_mae_pct"]),
    )
    comparison = {
        "significance": _significance_comparison(
            best,
            production,
            bootstrap_iterations=bootstrap_iterations,
            min_paired_samples=min_paired_samples,
        )
    }
    optimizer_report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_source": "frozen forecast samples",
        "candidates": [production, *recency_candidates],
        "comparison": comparison,
    }
    health: dict[str, Any] = {
        "available": True,
        "state_version": 1,
        "drift_severity": severity,
        "open_circuits": [],
        "overall_health": "healthy" if severity == "none" else "degraded",
    }
    decision = evaluate_promotion(optimizer_report, health=health)
    champion = challenger_report.build_report(optimizer_report, decision)
    return {
        "available": True,
        "challenger": best["name"],
        "challenger_parameters": best["parameters"],
        "report_only": True,
        "review_contract": {
            "production_changes_automatic": False,
            "requires_human_review": True,
        },
        "decision": decision,
        "champion_challenger": champion,
        "candidate_metrics": best,
    }


def build_manifest(
    *,
    run_id: str | None,
    run_type: str,
    created_at: datetime | None,
    model_names: Sequence[str],
    policy_configs: Mapping[str, Any],
    leakage: Mapping[str, Any],
    drift_config: Mapping[str, Any],
    data_identity: Mapping[str, Any],
    git_sha: str | None = None,
) -> dict[str, Any]:
    """Build a reproducibility manifest consistent with experiment_registry."""
    created = created_at or datetime.now(timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    created = created.astimezone(timezone.utc)
    configuration: dict[str, Any] = {
        "forecast": {"model_names": sorted(model_names), "target_hours": list(TARGET_HOURS)},
        "adaptation_evaluation": {
            "policies": dict(policy_configs),
            "leakage": dict(leakage),
            "drift_config": dict(drift_config),
        },
    }
    configuration_hash = hashlib.sha256(
        json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    configuration_id = f"cfg-{configuration_hash[:20]}"
    data_id = f"data-{hashlib.sha256(json.dumps(data_identity, sort_keys=True).encode('utf-8')).hexdigest()[:20]}"
    if run_id is None:
        stamp = created.strftime("%Y%m%dT%H%M%SZ")
        run_id = f"recency-adaptation-{stamp}-{configuration_hash[:8]}-{data_id[-8:]}"
    return {
        "manifest_version": 1,
        "run_id": run_id,
        "run_type": run_type,
        "created_at": created.isoformat(),
        "configuration_id": configuration_id,
        "data_id": data_id,
        "code": {"git_sha": git_sha, "dirty": None},
        "configuration": configuration,
        "data": data_identity,
    }


def register_evaluation(
    report: Mapping[str, Any],
    *,
    db_path: Path,
    decision: str = "candidate",
) -> dict[str, Any]:
    """Catalog the evaluation in the experiment registry (first write wins)."""
    manifest = report.get("experiment_manifest")
    if not isinstance(manifest, dict):
        raise ValueError("recency-adaptation report has no experiment_manifest")
    metrics = report.get("metrics_for_registry")
    evidence = report.get("significance")
    registry = ExperimentRegistry(db_path)
    record, created = registry.register_from_manifest(
        manifest,
        metrics=dict(metrics) if isinstance(metrics, dict) else None,
        statistical_evidence=dict(evidence) if isinstance(evidence, dict) else None,
        decision=decision,
        hypothesis="evaluate bounded online and recency-aware adaptation (#130)",
    )
    return {"created": created, "run_id": record["run_id"], "record": record}


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Recency-aware adaptation evaluation",
        "",
        f"- Evaluation: **{report.get('evaluation', 'recency_adaptation')}** "
        f"({report.get('origin_count')} validation origins)",
        f"- Leakage safety: origin-time-only actuals + purged walk-forward "
        f"= **{report.get('leakage_safety', {}).get('assert_fold_leakage')}**",
    ]
    drift = report.get("drift_segmentation", {})
    counts = drift.get("origins", {})
    lines.append(
        f"- Drift periods: **{counts.get('drift', 0)}** origins, "
        f"normal periods: **{counts.get('normal', 0)}** origins"
    )
    promotion = report.get("promotion", {})
    if promotion.get("available"):
        decision = promotion.get("decision", {})
        lines.append(
            f"- Promotion verdict: **{str(decision.get('decision', 'unknown')).upper()}** "
            f"for challenger `{promotion.get('challenger')}` (report-only)"
        )
    lines.extend(
        [
            "",
            "## Overall MAE by horizon (%)",
            "",
            "| Horizon | Production | Persistence |",
            "| --- | ---: | ---: |",
        ]
    )
    for horizon, block in (report.get("by_horizon") or {}).items():
        production = block.get("production_adaptive", {})
        persistence = block.get("persistence", {})
        lines.append(
            f"| {horizon} | {_fmt(production.get('mae_pct'))} | {_fmt(persistence.get('mae_pct'))} |"
        )
        for name, metrics in (block.get("policies") or {}).items():
            lines.append(
                f"  - `{name}` MAE {_fmt(metrics.get('mae_pct'))} "
                f"(delta vs production {_fmt(block.get('policy_mae_delta_vs_production_pct', {}).get(name))})"
            )
    return "\n".join(lines).rstrip() + "\n"


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate bounded online and recency-aware adaptation on frozen forecasts"
    )
    parser.add_argument("--samples-json", type=Path, required=True)
    parser.add_argument("--actuals-json", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=REPORT_PATH)
    parser.add_argument("--markdown", type=Path, default=Path("recency_adaptation_summary.md"))
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--folds", type=int, default=3)
    parser.add_argument("--history-limit", type=int, default=DEFAULT_HISTORY_LIMIT)
    args = parser.parse_args()

    samples = json.loads(args.samples_json.read_text(encoding="utf-8"))
    raw_actuals = json.loads(args.actuals_json.read_text(encoding="utf-8"))
    if not isinstance(samples, list) or not isinstance(raw_actuals, dict):
        raise ValueError("samples must be a list and actuals must be an object")
    actuals = {int(key): float(value) for key, value in raw_actuals.items()}
    report = evaluate_recency_adaptation(
        [item for item in samples if isinstance(item, dict)],
        actuals,
        folds=args.folds,
        history_limit=args.history_limit,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.markdown.write_text(render_markdown(report), encoding="utf-8")
    print(render_markdown(report))
    registration: dict[str, Any] | None = None
    if args.db is not None:
        registration = register_evaluation(report, db_path=args.db)
    print(f"Saved {args.output} and {args.markdown}")
    if registration is not None:
        print(
            f"Registered experiment {registration['run_id']} "
            f"(created={registration['created']}) in {args.db}"
        )


if __name__ == "__main__":
    main()
