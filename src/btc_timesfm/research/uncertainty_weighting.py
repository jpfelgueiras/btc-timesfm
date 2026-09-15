#!/usr/bin/env python3
"""Uncertainty-aware ensemble weighting evaluation (issue #131).

The production ensemble already blends out-of-sample error history (adaptive
weighting) and applies a residual-correlation diversification overlay. This
module adds a *calibrated-uncertainty* overlay: models whose matured Q10-Q90
intervals are miscalibrated - especially overconfident models that are narrow
but under-covering - are penalized using only outcomes available at each
forecast origin. A directional (Brier) calibration component is combined the
same way so both interval and probability calibration influence the final
weight.

All uncertainty inputs are calibrated out of sample: the weighting replay
exposes only matured outcomes whose target timestamp is no later than the
forecast origin. The weight formula, its safeguards and every hyperparameter
are versioned in a JSON-serializable block embedded in every report and every
registered experiment manifest, so results are reproducible. The current
adaptive/correlation-aware ensemble remains the mandatory baseline and this
module never changes production defaults (evaluation is report-only and routed
through the existing paired-bootstrap significance policy).

Safeguards:
- Weights stay inside the existing ``ADAPTIVE_MIN_WEIGHT``/``ADAPTIVE_MAX_WEIGHT``
  bounds via ``_bounded_normalize``.
- Sparse calibration history falls back to the base policy unchanged.
- A dominance guard prevents a single overconfident or thinly-evidenced model
  from holding a lead larger than ``MAX_TOTAL_DOMINANCE_RATIO`` over the
  runner-up unless it has both enough matured samples and calibrated intervals.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from btc_timesfm.forecasting.adaptive_weighting import (
    adaptive_model_weights as base_adaptive_model_weights,
)
from btc_timesfm.forecasting.correlation_weighting import correlation_aware_model_weights
from btc_timesfm.forecasting.forecast_engine import (
    ADAPTIVE_MAX_WEIGHT,
    ADAPTIVE_MIN_WEIGHT,
    TARGET_HOURS,
    _bounded_normalize,
)
from btc_timesfm.forecasting.statistical_significance import paired_bootstrap_comparison
from btc_timesfm.research.experiment_registry import ExperimentRegistry

UNCERTAINTY_WEIGHTING_VERSION = 1
DEFAULT_UNCERTAINTY_HISTORY_LIMIT = int(os.getenv("BTC_UNCERTAINTY_HISTORY_LIMIT", "200"))
TARGET_INTERVAL_COVERAGE = float(os.getenv("BTC_UNCERTAINTY_TARGET_COVERAGE", "0.80"))
MIN_CALIBRATION_SAMPLES = int(os.getenv("BTC_UNCERTAINTY_MIN_SAMPLES", "6"))
CALIBRATION_FULL_SAMPLES = int(os.getenv("BTC_UNCERTAINTY_FULL_SAMPLES", "24"))
UNCERTAINTY_PENALTY_STRENGTH = float(os.getenv("BTC_UNCERTAINTY_PENALTY_STRENGTH", "0.60"))
MAX_CALIBRATION_BLEND = float(os.getenv("BTC_UNCERTAINTY_MAX_BLEND", "0.70"))
OVERCONFIDENCE_COVERAGE_GAP = float(os.getenv("BTC_UNCERTAINTY_OVERCONFIDENCE_GAP", "0.15"))
DIRECTION_CALIBRATION_WEIGHT = float(os.getenv("BTC_UNCERTAINTY_DIRECTION_WEIGHT", "0.50"))
MAX_TOTAL_DOMINANCE_RATIO = float(os.getenv("BTC_UNCERTAINTY_MAX_DOMINANCE_RATIO", "2.0"))
MIN_DOMINANCE_EVIDENCE_SAMPLES = int(os.getenv("BTC_UNCERTAINTY_MIN_EVIDENCE_SAMPLES", "32"))
PROMOTION_MODE = "report_only"
POLICY_NAMES = ("adaptive", "correlation_aware", "uncertainty_aware", "persistence")
DEFAULT_BOOTSTRAP_ITERATIONS = 1000
DEFAULT_MIN_PAIRED_SAMPLES = 32
PERSISTENCE_MODEL = "persistence"
_EPSILON = 1e-9


def weighting_formula() -> dict[str, Any]:
    """Return the versioned, JSON-serializable weight formula and safeguards."""
    return {
        "version": UNCERTAINTY_WEIGHTING_VERSION,
        "description": (
            "adaptive out-of-sample MAE/direction/base scores are scaled by a "
            "sample-shrunk calibrated-uncertainty penalty combining interval "
            "coverage error and directional Brier calibration, then correlation-"
            "aware redundancy and bounded normalization are reapplied with a "
            "dominance guard"
        ),
        "base_policy": "adaptive_weighting.adaptive_model_weights",
        "correlation_overlay": "correlation_weighting.correlation_aware_model_weights",
        "target_interval_coverage": TARGET_INTERVAL_COVERAGE,
        "min_calibration_samples": MIN_CALIBRATION_SAMPLES,
        "calibration_full_samples": CALIBRATION_FULL_SAMPLES,
        "uncertainty_penalty_strength": UNCERTAINTY_PENALTY_STRENGTH,
        "max_calibration_blend": MAX_CALIBRATION_BLEND,
        "overconfidence_coverage_gap": OVERCONFIDENCE_COVERAGE_GAP,
        "direction_calibration_weight": DIRECTION_CALIBRATION_WEIGHT,
        "max_total_dominance_ratio": MAX_TOTAL_DOMINANCE_RATIO,
        "min_dominance_evidence_samples": MIN_DOMINANCE_EVIDENCE_SAMPLES,
        "weight_floor": ADAPTIVE_MIN_WEIGHT,
        "weight_cap": ADAPTIVE_MAX_WEIGHT,
        "bounded_normalize": "adaptive_weighting._bounded_normalize",
        "promotion_mode": PROMOTION_MODE,
        "evidence_policy": "paired_bootstrap_comparison",
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return float(sum(items) / len(items)) if items else None


def _std(values: Iterable[float]) -> float | None:
    items = list(values)
    if len(items) < 2:
        return None
    return float(np.std(items, ddof=1))


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None else None


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
    candle_actual = actual_by_timestamp.get(int(origin.timestamp()) + hour * 3600)
    if candle_actual is None:
        return None
    value = float(candle_actual)
    return value if value > 0 else None


def _smoothed_rate(hits: int, total: int, prior: float = 0.5, strength: float = 1.0) -> float:
    return (hits + strength * prior) / (total + strength)


def _brier(error_pairs: list[tuple[float, int]]) -> float | None:
    if not error_pairs:
        return None
    return float(
        sum((p_value - outcome) ** 2 for p_value, outcome in error_pairs) / len(error_pairs)
    )


def calibrated_uncertainty(
    history: list[dict[str, Any]],
    actual_by_timestamp: dict[int, float],
    model_names: list[str],
    hour: int,
    *,
    history_limit: int = DEFAULT_UNCERTAINTY_HISTORY_LIMIT,
    target_coverage: float = TARGET_INTERVAL_COVERAGE,
    min_samples: int = MIN_CALIBRATION_SAMPLES,
    overconfidence_gap: float = OVERCONFIDENCE_COVERAGE_GAP,
) -> dict[str, dict[str, Any]]:
    """Compute leakage-safe calibrated interval/probability uncertainty per model.

    Only matured outcomes whose target predates the caller's cutoff are used.
    For each model the interval component is the empirical Q10-Q90 coverage of
    the model's own intervals; the probability component is the leave-one-out
    Brier error of the smoothed historical P(up) for that model+horizon.
    """
    horizon = f"{hour}h"
    intervals: dict[str, list[dict[str, float]]] = {name: [] for name in model_names}
    directions: dict[str, list[float]] = {name: [] for name in model_names}
    complete_origins = 0
    for snapshot in reversed(history):
        origin = _origin(snapshot)
        if origin is None:
            continue
        try:
            previous_close = float(snapshot["latest_close_usd"])
        except (KeyError, TypeError, ValueError):
            previous_close = float("nan")
        had_sample = False
        for name in model_names:
            try:
                item = snapshot["model_predictions"][name][horizon]
                point = float(item["price_usd"])
            except (KeyError, TypeError, ValueError):
                continue
            actual = _actual(snapshot, horizon, name, hour, actual_by_timestamp)
            if actual is None:
                continue
            if math.isfinite(previous_close):
                directions[name].append(1.0 if actual > previous_close else 0.0)
            try:
                q10 = float(item["q10_usd"])
                q90 = float(item["q90_usd"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (math.isfinite(q10) and math.isfinite(q90) and q90 > q10 and point > 0):
                continue
            intervals[name].append(
                {
                    "point": point,
                    "q10": q10,
                    "q90": q90,
                    "covered": float(q10 <= actual <= q90),
                }
            )
            had_sample = True
        if had_sample:
            complete_origins += 1
            if complete_origins >= history_limit:
                break

    result: dict[str, dict[str, Any]] = {}
    for name in model_names:
        interval_rows = intervals[name]
        interval_count = len(interval_rows)
        coverage: float | None = None
        coverage_error: float | None = None
        width_pct: float | None = None
        if interval_count:
            coverage = float(np.mean([row["covered"] for row in interval_rows]))
            coverage_error = abs(coverage - target_coverage)
            width_pct = float(
                np.mean([(row["q90"] - row["q10"]) / row["point"] * 100.0 for row in interval_rows])
            )

        direction_rows = directions[name]
        brier_error: float | None = None
        if len(direction_rows) >= min_samples and interval_count == 0:
            total = len(direction_rows)
            total_up = int(sum(direction_rows))
            pairs: list[tuple[float, int]] = [
                (_smoothed_rate(total_up - int(outcome), total - 1), int(outcome))
                for outcome in direction_rows
            ]
            brier_error = _brier(pairs)

        overconfident = bool(
            interval_count >= min_samples
            and coverage is not None
            and coverage < target_coverage - overconfidence_gap
        )
        result[name] = {
            "samples": interval_count,
            "interval_samples": interval_count,
            "direction_samples": len(direction_rows),
            "coverage": _round(coverage),
            "coverage_error": _round(coverage_error),
            "interval_width_pct": _round(width_pct),
            "direction_brier_error": _round(brier_error),
            "overconfident": overconfident,
            "calibration_state": "calibrated"
            if interval_count >= min_samples
            else "sparse_fallback",
            "target_coverage": target_coverage,
        }
    return result


def uncertainty_adjustments(
    uncertainty: dict[str, dict[str, Any]],
    *,
    min_samples: int = MIN_CALIBRATION_SAMPLES,
    full_samples: int = CALIBRATION_FULL_SAMPLES,
    penalty_strength: float = UNCERTAINTY_PENALTY_STRENGTH,
    max_blend: float = MAX_CALIBRATION_BLEND,
    direction_weight: float = DIRECTION_CALIBRATION_WEIGHT,
) -> tuple[dict[str, float], dict[str, dict[str, Any]]]:
    """Turn calibrated-uncertainty metrics into sample-shrunk penalties (<= 1)."""
    penalties: dict[str, float] = {}
    diagnostics: dict[str, dict[str, Any]] = {}
    for name, info in uncertainty.items():
        samples = int(info.get("samples") or 0)
        steps = info.get("calibration_state") == "calibrated"
        components: list[tuple[str, float]] = []
        coverage_error = info.get("coverage_error")
        if isinstance(coverage_error, (int, float)) and math.isfinite(float(coverage_error)):
            components.append(("interval_coverage_error", float(coverage_error)))
        brier_error = info.get("direction_brier_error")
        if isinstance(brier_error, (int, float)) and math.isfinite(float(brier_error)):
            components.append(("direction_brier_error", direction_weight * float(brier_error)))

        if not steps or not components:
            penalties[name] = 1.0
            diagnostics[name] = {
                "samples": samples,
                "calibration_state": "sparse_fallback",
                "components": [name for name, _ in components],
                "calibration_error": None,
                "blend": 0.0,
                "penalty": 1.0,
            }
            continue

        weights = [1.0] * len(components)
        calibration_error = sum(
            w * value for (_, value), w in zip(components, weights, strict=True)
        ) / sum(weights)
        progress = min(1.0, max(0.0, (samples - min_samples) / max(1, full_samples - min_samples)))
        blend = max_blend * progress
        raw_penalty = math.exp(-penalty_strength * calibration_error)
        penalty = 1.0 - blend * (1.0 - raw_penalty)
        penalties[name] = penalty
        diagnostics[name] = {
            "samples": samples,
            "calibration_state": "calibrated",
            "components": [name for name, _ in components],
            "calibration_error": round(calibration_error, 6),
            "progress": round(progress, 6),
            "blend": round(blend, 6),
            "raw_penalty": round(raw_penalty, 6),
            "penalty": round(penalty, 6),
        }
    return penalties, diagnostics


def dominance_guard(
    raw: dict[str, float],
    uncertainty: dict[str, dict[str, Any]],
    *,
    max_ratio: float = MAX_TOTAL_DOMINANCE_RATIO,
    min_evidence_samples: int = MIN_DOMINANCE_EVIDENCE_SAMPLES,
    min_score: float = 1e-9,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Stop a single overconfident or thinly-evidenced model from dominating.

    A model may hold a lead larger than ``max_ratio`` over the runner-up only
    when it has at least ``min_evidence_samples`` matured samples and is not
    flagged overconfident. Otherwise its adjusted raw weight is clipped to
    ``runner_up * max_ratio`` before bounded normalization.
    """
    guarded = dict(raw)
    ordered = sorted(raw, key=lambda name: float(raw[name]), reverse=True)
    if len(ordered) < 2:
        return guarded, {"applied": False, "reason": "single_model", "clipped_models": []}
    best, second = ordered[0], ordered[1]
    info = uncertainty.get(best, {})
    has_evidence = int(info.get("samples") or 0) >= min_evidence_samples and not bool(
        info.get("overconfident", False)
    )
    ratio = float(raw[best]) / max(float(raw[second]), min_score)
    clipped: list[str] = []
    if ratio > max_ratio and not has_evidence:
        guarded[best] = float(raw[second]) * max_ratio
        clipped = [best]
    return guarded, {
        "applied": bool(clipped),
        "reason": "leader_reverted_to_evidence" if clipped else "lead_within_bounds",
        "leader": best,
        "leader_evidence_samples": int(info.get("samples") or 0),
        "overconfident_leader": bool(info.get("overconfident", False)),
        "max_ratio": max_ratio,
        "raw_ratio": round(ratio, 6),
        "clipped_models": clipped,
    }


def uncertainty_aware_model_weights(
    model_names: list[str],
    regime: str,
    hour: int,
    history: list[dict[str, Any]],
    actual_by_timestamp: dict[int, float],
    enabled: bool = True,
    history_limit: int | None = None,
    confidence: float = 1.0,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Apply a calibrated-uncertainty overlay on the correlation-aware policy.

    The candidate stacks the existing adaptive weights (out-of-sample error
    history), the residual-correlation diversification overlay, and then this
    module's sample-shrunk calibrated-uncertainty penalties, each inside the
    existing floor/cap normalization, with a dominance guard as the final
    safeguard.
    """
    base_weights, base_diagnostics = correlation_aware_model_weights(
        model_names,
        regime,
        hour,
        history,
        actual_by_timestamp,
        enabled=enabled,
        history_limit=history_limit,
        confidence=confidence,
    )
    limit = max(MIN_CALIBRATION_SAMPLES, int(history_limit or DEFAULT_UNCERTAINTY_HISTORY_LIMIT))
    uncertainty = calibrated_uncertainty(
        history,
        actual_by_timestamp,
        model_names,
        hour,
        history_limit=limit,
    )
    penalties, penalty_diagnostics = uncertainty_adjustments(uncertainty)
    base_mode = str(base_diagnostics.get("mode"))

    if not enabled or base_mode == "static_prior":
        diagnostics = {
            **base_diagnostics,
            "base_policy_mode": base_mode,
            "uncertainty_mode": "base_policy",
            "uncertainty_reason": ("disabled" if not enabled else "base_policy_not_adaptive"),
            "calibrated_uncertainty": uncertainty,
            "uncertainty_penalties": penalty_diagnostics,
            "dominance_guard": {"applied": False, "reason": "base_policy", "clipped_models": []},
            "weighting_formula": weighting_formula(),
        }
        return base_weights, diagnostics

    adjusted_raw = {name: base_weights[name] * penalties[name] for name in model_names}
    guarded_raw, guard = dominance_guard(adjusted_raw, uncertainty)
    final = _bounded_normalize(guarded_raw, ADAPTIVE_MIN_WEIGHT, ADAPTIVE_MAX_WEIGHT)
    diagnostics = {
        **base_diagnostics,
        "base_policy_mode": base_mode,
        "mode": "uncertainty_aware_adaptive",
        "uncertainty_mode": "active",
        "calibrated_uncertainty": uncertainty,
        "uncertainty_penalties": penalty_diagnostics,
        "dominance_guard": guard,
        "uncertainty_models": {
            name: {
                **penalty_diagnostics[name],
                "final_weight": round(final[name], 6),
                "weight_delta": round(final[name] - base_weights[name], 6),
            }
            for name in model_names
        },
        "weighting_formula": weighting_formula(),
    }
    return final, diagnostics


def _history_snapshot(sample: dict[str, Any]) -> dict[str, Any]:
    forecast = sample["forecast"]
    return {
        "latest_close_at": forecast["latest_close_at"],
        "latest_close_usd": forecast["latest_close_usd"],
        "regime": forecast["regime"],
        "model_predictions": forecast["model_predictions"],
        "predictions": forecast.get("predictions", {}),
        "_outcomes": forecast.get("_outcomes", {}),
    }


def _weighted_ensemble_price(
    current: float,
    model_predictions: dict[str, dict[str, dict[str, Any]]],
    horizon: str,
    weights: dict[str, float],
) -> float:
    """Mirror production's normalized weighted geometric price ensemble."""
    weighted_log_changes = 0.0
    total_weight = 0.0
    for name, weight in weights.items():
        if weight <= 0:
            continue
        price = float(model_predictions[name][horizon]["price_usd"])
        weighted_log_changes += weight * math.log(price / current)
        total_weight += weight
    if total_weight <= 0:
        raise ValueError("ensemble weights must contain a positive value")
    return current * math.exp(weighted_log_changes / total_weight)


def _policy_interval(
    model_predictions: dict[str, dict[str, dict[str, Any]]],
    horizon: str,
    weights: dict[str, float],
    current: float,
) -> tuple[float, float, float] | None:
    """Weighted average of model Q10/Q90 bounds; None when no model has them."""
    total = 0.0
    lower_sum = 0.0
    upper_sum = 0.0
    for name, weight in weights.items():
        if weight <= 0:
            continue
        item = model_predictions.get(name, {}).get(horizon, {})
        q10 = item.get("q10_usd")
        q90 = item.get("q90_usd")
        if q10 is None or q90 is None:
            continue
        try:
            lower = float(q10)
            upper = float(q90)
            weight_value = float(weight)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lower) and math.isfinite(upper) and upper > lower):
            continue
        total += weight_value
        lower_sum += weight_value * lower
        upper_sum += weight_value * upper
    if total <= 0:
        return None
    lower = lower_sum / total
    upper = upper_sum / total
    width_pct = (upper - lower) / current * 100.0 if current > 0 else None
    return lower, upper, width_pct if width_pct is not None else 0.0


def _score(current: float, predicted: float, actual: float) -> dict[str, float]:
    predicted_change = predicted - current
    actual_change = actual - current
    return {
        "absolute_error_pct": abs(predicted - actual) / actual * 100.0,
        "direction_correct": float(
            (predicted_change > 0 and actual_change > 0)
            or (predicted_change < 0 and actual_change < 0)
            or (predicted_change == 0 and actual_change == 0)
        ),
    }


def _policy_metrics(
    rows: list[dict[str, Any]],
    target_coverage: float = TARGET_INTERVAL_COVERAGE,
) -> dict[str, Any]:
    if not rows:
        return {
            "samples": 0,
            "mae_pct": None,
            "direction_accuracy": None,
            "coverage": None,
            "coverage_error": None,
            "interval_samples": 0,
            "interval_width_pct": None,
        }
    covered = [row["covered"] for row in rows if row["interval_available"]]
    coverage = _mean([float(value) for value in covered]) if covered else None
    widths = [
        float(row["interval_width_pct"])
        for row in rows
        if row["interval_available"] and row["interval_width_pct"] is not None
    ]
    return {
        "samples": len(rows),
        "mae_pct": _round(_mean([float(row["error_pct"]) for row in rows])),
        "direction_accuracy": _round(_mean([float(row["direction_correct"]) for row in rows])),
        "coverage": _round(coverage),
        "coverage_error": _round(abs(coverage - target_coverage)) if coverage is not None else None,
        "interval_samples": len(covered),
        "interval_width_pct": _round(_mean(widths)),
    }


def _bucketed_stability(
    rows_by_policy: dict[str, list[dict[str, Any]]],
    max_buckets: int = 4,
) -> dict[str, Any]:
    sizes = [len(entries) for entries in rows_by_policy.values()]
    total = sizes[0] if sizes else 0
    bucket_count = max(1, min(max_buckets, total // 4)) if total else 0
    per_policy_mae: dict[str, list[float | None]] = {
        policy: [] if bucket_count else [] for policy in rows_by_policy
    }
    if bucket_count == 0:
        return {
            "bucket_count": 0,
            "per_policy_bucket_mae": per_policy_mae,
            "uncertainty_mae_std": None,
            "better_than_correlation_buckets": None,
        }
    for policy, entries in rows_by_policy.items():
        ordered_entries = sorted(entries, key=lambda row: int(row["timestamp"]))
        chunk = max(1, math.ceil(len(ordered_entries) / bucket_count))
        per_policy_mae[policy] = [
            _round(
                _mean([float(row["error_pct"]) for row in ordered_entries[start : start + chunk]])
            )
            for start in range(0, len(ordered_entries), chunk)
        ]
    uncertainty_mae = per_policy_mae.get("uncertainty_aware") or []
    correlation_mae = per_policy_mae.get("correlation_aware") or []
    better = sum(
        unc is not None and corr is not None and unc < corr
        for unc, corr in zip(uncertainty_mae, correlation_mae, strict=False)
    )
    return {
        "bucket_count": bucket_count,
        "per_policy_bucket_mae": per_policy_mae,
        "uncertainty_mae_std": _round(_std([v for v in uncertainty_mae if v is not None])),
        "better_than_correlation_buckets": better if uncertainty_mae and correlation_mae else None,
    }


def _significance(
    candidate_rows: list[dict[str, Any]],
    baseline_rows: list[dict[str, Any]],
    *,
    metric: str,
    lower_is_better: bool,
    bootstrap_iterations: int,
    min_paired_samples: int,
) -> dict[str, Any]:
    if len(candidate_rows) != len(baseline_rows):
        return {
            "metric": metric,
            "samples": 0,
            "conclusion": "inconclusive",
            "reason": "unpaired_samples",
        }
    return paired_bootstrap_comparison(
        [float(row["error_pct"]) for row in candidate_rows],
        [float(row["error_pct"]) for row in baseline_rows],
        metric=metric,
        lower_is_better=lower_is_better,
        iterations=bootstrap_iterations,
        min_samples=min_paired_samples,
    )


def evaluate_uncertainty_weighting(
    samples: list[dict[str, Any]],
    actual_by_timestamp: dict[int, float],
    *,
    history_limit: int = 200,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    min_paired_samples: int = DEFAULT_MIN_PAIRED_SAMPLES,
    target_coverage: float = TARGET_INTERVAL_COVERAGE,
) -> dict[str, Any]:
    """Replay adaptive, correlation-aware, uncertainty-aware and persistence on
    identical frozen origins and compare performance, stability and calibration
    by horizon and regime."""
    if history_limit < 1:
        raise ValueError("history_limit must be positive")
    if bootstrap_iterations < 100:
        raise ValueError("bootstrap_iterations must be at least 100")
    if min_paired_samples < 1:
        raise ValueError("min_paired_samples must be positive")
    if not 0.0 < target_coverage < 1.0:
        raise ValueError("target_coverage must be between 0 and 1")

    current_time = datetime.now(timezone.utc)
    ordered = sorted(samples, key=lambda item: int(item["origin_timestamp"]))
    history: list[dict[str, Any]] = []
    rows: dict[str, dict[str, list[dict[str, Any]]]] = {
        policy: {f"{hour}h": [] for hour in TARGET_HOURS} for policy in POLICY_NAMES
    }
    model_names_all: set[str] = set()
    origins: list[tuple[int, str]] = []
    correlation_modes: dict[str, list[str]] = {f"{hour}h": [] for hour in TARGET_HOURS}
    uncertainty_modes: dict[str, list[str]] = {f"{hour}h": [] for hour in TARGET_HOURS}
    guard_applied: dict[str, int] = {f"{hour}h": 0 for hour in TARGET_HOURS}

    for sample in ordered:
        timestamp = int(sample["origin_timestamp"])
        current = float(sample["current_price"])
        forecast = sample["forecast"]
        regime = str(forecast.get("regime") or "unknown")
        model_predictions = forecast["model_predictions"]
        model_names = list(model_predictions)
        model_names_all.update(model_names)
        sample_actuals: dict[str, Any] = {}
        raw_sample_actuals = sample.get("actuals")
        if isinstance(raw_sample_actuals, dict):
            sample_actuals = {str(key): value for key, value in raw_sample_actuals.items()}
        visible_actuals = {
            target: value
            for target, value in actual_by_timestamp.items()
            if int(target) <= timestamp
        }
        origins.append((timestamp, regime))

        for hour in TARGET_HOURS:
            horizon = f"{hour}h"
            raw_actual = sample_actuals.get(horizon)
            if raw_actual is None:
                continue
            actual = float(raw_actual)
            if not math.isfinite(actual):
                continue

            adaptive_weights, _ = base_adaptive_model_weights(
                model_names, regime, hour, history, visible_actuals, history_limit=history_limit
            )
            correlation_weights, correlation_diagnostics = correlation_aware_model_weights(
                model_names, regime, hour, history, visible_actuals, history_limit=history_limit
            )
            uncertainty_weights, uncertainty_diagnostics = uncertainty_aware_model_weights(
                model_names, regime, hour, history, visible_actuals, history_limit=history_limit
            )
            correlation_modes[horizon].append(str(correlation_diagnostics.get("correlation_mode")))
            uncertainty_modes[horizon].append(str(uncertainty_diagnostics.get("uncertainty_mode")))
            if bool(uncertainty_diagnostics.get("dominance_guard", {}).get("applied", False)):
                guard_applied[horizon] += 1

            policies: dict[str, dict[str, float]] = {
                "adaptive": adaptive_weights,
                "correlation_aware": correlation_weights,
                "uncertainty_aware": uncertainty_weights,
            }
            if PERSISTENCE_MODEL in model_predictions:
                policies[PERSISTENCE_MODEL] = {PERSISTENCE_MODEL: 1.0}

            for policy, weights in policies.items():
                price = _weighted_ensemble_price(current, model_predictions, horizon, weights)
                score = _score(current, price, actual)
                interval = _policy_interval(model_predictions, horizon, weights, current)
                rows[policy][horizon].append(
                    {
                        "timestamp": timestamp,
                        "regime": regime,
                        "error_pct": score["absolute_error_pct"],
                        "direction_correct": score["direction_correct"],
                        "interval_available": interval is not None,
                        "covered": (
                            float(interval[0] <= actual <= interval[1])
                            if interval is not None
                            else None
                        ),
                        "interval_width_pct": interval[2] if interval is not None else None,
                    }
                )

        history.append(_history_snapshot(sample))
        history = history[-history_limit:]

    by_horizon: dict[str, Any] = {}
    for hour in TARGET_HOURS:
        horizon = f"{hour}h"
        horizon_rows = {policy: rows[policy][horizon] for policy in POLICY_NAMES}
        per_policy = {
            policy: _policy_metrics(entries, target_coverage)
            for policy, entries in horizon_rows.items()
        }
        by_horizon[horizon] = {
            "policies": per_policy,
            "uncertainty_minus_correlation_mae_pct": _mae_delta(per_policy),
            "active_correlation_origins": sum(
                mode == "active" for mode in correlation_modes[horizon]
            ),
            "active_uncertainty_origins": sum(
                mode == "active" for mode in uncertainty_modes[horizon]
            ),
            "dominance_guard_origins": guard_applied[horizon],
            "stability": _bucketed_stability(horizon_rows),
        }

    regimes = sorted({regime for _, regime in origins})
    by_regime: dict[str, Any] = {}
    for regime in regimes:
        regime_horizons: dict[str, Any] = {}
        for hour in TARGET_HOURS:
            horizon = f"{hour}h"
            regime_rows = {
                policy: [row for row in rows[policy][horizon] if row["regime"] == regime]
                for policy in POLICY_NAMES
            }
            if all(not entries for entries in regime_rows.values()):
                continue
            per_policy = {
                policy: _policy_metrics(entries, target_coverage)
                for policy, entries in regime_rows.items()
            }
            regime_horizons[horizon] = {
                "policies": per_policy,
                "uncertainty_minus_correlation_mae_pct": _mae_delta(per_policy),
            }
        if regime_horizons:
            pooled = {
                policy: [
                    row
                    for hour in TARGET_HOURS
                    for row in rows[policy][f"{hour}h"]
                    if row["regime"] == regime
                ]
                for policy in POLICY_NAMES
            }
            by_regime[regime] = {
                "horizons": regime_horizons,
                "stability": _bucketed_stability(pooled),
            }

    significance: dict[str, Any] = {}
    for hour in TARGET_HOURS:
        horizon = f"{hour}h"
        significance[horizon] = {
            "uncertainty_vs_correlation_aware": _significance(
                rows["uncertainty_aware"][horizon],
                rows["correlation_aware"][horizon],
                metric="mae_pct",
                lower_is_better=True,
                bootstrap_iterations=bootstrap_iterations,
                min_paired_samples=min_paired_samples,
            ),
            "uncertainty_vs_adaptive": _significance(
                rows["uncertainty_aware"][horizon],
                rows["adaptive"][horizon],
                metric="mae_pct",
                lower_is_better=True,
                bootstrap_iterations=bootstrap_iterations,
                min_paired_samples=min_paired_samples,
            ),
            "uncertainty_vs_persistence": _significance(
                rows["uncertainty_aware"][horizon],
                rows[PERSISTENCE_MODEL][horizon],
                metric="mae_pct",
                lower_is_better=True,
                bootstrap_iterations=bootstrap_iterations,
                min_paired_samples=min_paired_samples,
            ),
        }
    significance["overall"] = {
        "uncertainty_vs_correlation_aware": _significance(
            [row for hour in TARGET_HOURS for row in rows["uncertainty_aware"][f"{hour}h"]],
            [row for hour in TARGET_HOURS for row in rows["correlation_aware"][f"{hour}h"]],
            metric="mae_pct",
            lower_is_better=True,
            bootstrap_iterations=bootstrap_iterations,
            min_paired_samples=min_paired_samples,
        ),
        "uncertainty_vs_persistence": _significance(
            [row for hour in TARGET_HOURS for row in rows["uncertainty_aware"][f"{hour}h"]],
            [row for hour in TARGET_HOURS for row in rows[PERSISTENCE_MODEL][f"{hour}h"]],
            metric="mae_pct",
            lower_is_better=True,
            bootstrap_iterations=bootstrap_iterations,
            min_paired_samples=min_paired_samples,
        ),
    }

    first_origin = origins[0][0] if origins else None
    last_origin = origins[-1][0] if origins else None
    report_core: dict[str, Any] = {
        "schema_version": UNCERTAINTY_WEIGHTING_VERSION,
        "samples": len(ordered),
        "history_limit": history_limit,
        "horizons": [f"{hour}h" for hour in TARGET_HOURS],
        "model_names": sorted(model_names_all),
        "regimes": regimes,
        "evaluation_window": {
            "start": (
                datetime.fromtimestamp(first_origin, tz=timezone.utc).isoformat()
                if first_origin is not None
                else None
            ),
            "end": (
                datetime.fromtimestamp(last_origin, tz=timezone.utc).isoformat()
                if last_origin is not None
                else None
            ),
            "origins": len(ordered),
        },
        "candidate": "uncertainty_aware_weighting",
        "mandatory_baselines": [
            "adaptive_weighting",
            "correlation_weighting",
            PERSISTENCE_MODEL,
        ],
        "promotion": {
            "mode": PROMOTION_MODE,
            "policy": "report_only_no_production_change",
            "evidence_policy": "paired_bootstrap_comparison",
        },
        "weighting_formula": weighting_formula(),
        "by_horizon": by_horizon,
        "by_regime": by_regime,
        "significance": significance,
        "stability": {
            "mode": "chronological_buckets",
            "by_horizon": {
                f"{hour}h": by_horizon[f"{hour}h"]["stability"] for hour in TARGET_HOURS
            },
        },
        "leakage_guard": {
            "evaluation": "frozen_out_of_sample_origins",
            "matured_outcomes_only": True,
            "uncertainty_inputs": "calibrated_out_of_sample",
            "weight_inputs": [
                "adaptive_error_history",
                "residual_correlations",
                "calibrated_interval_coverage",
                "calibrated_direction_brier",
            ],
            "origin_cutoff": "visible_actuals_filtered_by_origin_timestamp",
        },
    }
    audit_sha256 = _sha256_text(_canonical_json(report_core))
    report = {
        **report_core,
        "generated_at": current_time.isoformat(),
        "reproducibility": {
            "source": "frozen_walk_forward_samples",
            "audit_sha256": audit_sha256,
            "audit_inputs": [
                "origin_timestamp",
                "current_price",
                "forecast.model_predictions",
                "forecast.regime",
                "sample.actuals",
                "actual_by_timestamp",
                "weighting_formula",
            ],
        },
    }
    return report


def _mae_delta(per_policy: dict[str, dict[str, Any]]) -> float | None:
    uncertainty_mae = per_policy["uncertainty_aware"]["mae_pct"]
    correlation_mae = per_policy["correlation_aware"]["mae_pct"]
    if uncertainty_mae is None or correlation_mae is None:
        return None
    return round(float(uncertainty_mae) - float(correlation_mae), 6)


def build_evaluation_manifest(
    report: dict[str, Any],
    *,
    git_sha: str | None = None,
    run_type: str = "uncertainty_weighting",
    feature_set_version: str | None = None,
) -> dict[str, Any]:
    """Build a versioned manifest identity for the uncertainty-weighting run."""
    configuration: dict[str, Any] = {
        "feature_set_version": feature_set_version,
        "forecast": {"model_names": report.get("model_names", [])},
        "run_parameters": {
            "weighting_formula": report["weighting_formula"],
            "history_limit": report["history_limit"],
        },
    }
    config_hash = _sha256_text(_canonical_json(report["weighting_formula"]))[:20]
    data_block = {
        "source": "frozen_walk_forward",
        "samples": report.get("samples"),
        "evaluation_window": report.get("evaluation_window"),
        "model_names": report.get("model_names"),
    }
    data_hash = _sha256_text(_canonical_json(data_block))[:20]
    return {
        "manifest_version": 1,
        "run_id": f"{run_type}-{config_hash[:8]}-{data_hash[:8]}",
        "run_type": run_type,
        "created_at": report["generated_at"],
        "configuration_id": f"cfg-{config_hash}",
        "data_id": f"data-{data_hash}",
        "code": {"git_sha": git_sha or "unknown", "dirty": None},
        "configuration": configuration,
        "data": data_block,
    }


def report_metrics(report: dict[str, Any]) -> dict[str, Any]:
    """Optimizer-shaped metrics block for the experiment registry leaderboard."""
    by_horizon: dict[str, Any] = {}
    for horizon in report["horizons"]:
        policy = report["by_horizon"][horizon]["policies"]["uncertainty_aware"]
        if int(policy["samples"] or 0) == 0:
            continue
        by_horizon[horizon] = {
            key: policy.get(key)
            for key in (
                "samples",
                "mae_pct",
                "direction_accuracy",
                "coverage",
                "coverage_error",
                "interval_width_pct",
            )
        }
    by_regime: dict[str, Any] = {}
    for regime, block in report["by_regime"].items():
        entry: dict[str, Any] = {}
        for horizon, horizon_block in block["horizons"].items():
            policy = horizon_block["policies"]["uncertainty_aware"]
            if int(policy["samples"] or 0) == 0:
                continue
            entry[horizon] = {
                key: policy.get(key)
                for key in (
                    "samples",
                    "mae_pct",
                    "direction_accuracy",
                    "coverage",
                    "coverage_error",
                    "interval_width_pct",
                )
            }
        if entry:
            by_regime[regime] = entry
    mae_values = [v["mae_pct"] for v in by_horizon.values() if v["mae_pct"] is not None]
    direction_values = [
        v["direction_accuracy"] for v in by_horizon.values() if v["direction_accuracy"] is not None
    ]
    return {
        "samples": report["samples"],
        "objective_mae_pct": _round(_mean(mae_values)),
        "mean_signed_error_pct": None,
        "mean_direction_accuracy": _round(_mean(direction_values)),
        "by_horizon": by_horizon,
        "by_regime": by_regime,
    }


def register_from_report(
    report: dict[str, Any],
    registry: ExperimentRegistry,
    *,
    git_sha: str | None = None,
    decision: str = "candidate",
    feature_set_version: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Register the evaluation as a candidate experiment (report-only decision)."""
    manifest = build_evaluation_manifest(
        report,
        git_sha=git_sha,
        feature_set_version=feature_set_version,
    )
    record, created = registry.register_from_manifest(
        manifest,
        metrics=report_metrics(report),
        statistical_evidence=report["significance"],
        decision=decision,
        hypothesis="uncertainty-aware ensemble weighting evaluation (#131)",
        report_path="uncertainty_weighting_report.json",
    )
    return manifest, record, created


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Uncertainty-aware ensemble weighting",
        "",
        f"Generated: `{report['generated_at']}`",
        f"Frozen out-of-sample origins: **{report['samples']}**",
        f"Candidate: **uncertainty_aware_weighting** vs mandatory baselines "
        f"{', '.join(report['mandatory_baselines'])}",
        f"Promotion mode: **{report['promotion']['mode']}** (report-only, "
        "production defaults unchanged)",
        "",
        "Negative MAE delta means the uncertainty-aware candidate beat the "
        "correlation-aware baseline. Positive improvement favors the candidate.",
        "",
        "## By horizon",
        "",
        "| Horizon | Samples | Uncert MAE | Corr MAE | Delta | Uncert Dir | Corr Dir | Uncert Cov | Corr Cov | Significance |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for horizon, block in report["by_horizon"].items():
        unc = block["policies"]["uncertainty_aware"]
        corr = block["policies"]["correlation_aware"]
        conclusion = report["significance"][horizon]["uncertainty_vs_correlation_aware"][
            "conclusion"
        ]
        lines.append(
            "| "
            + " | ".join(
                [
                    str(horizon),
                    str(unc["samples"]),
                    _fmt(unc["mae_pct"]),
                    _fmt(corr["mae_pct"]),
                    _fmt(block["uncertainty_minus_correlation_mae_pct"]),
                    _fmt(unc["direction_accuracy"]),
                    _fmt(corr["direction_accuracy"]),
                    _fmt(unc["coverage"]),
                    _fmt(corr["coverage"]),
                    str(conclusion),
                ]
            )
            + " |"
        )
    lines.extend(["", "## By regime", ""])
    regime_header = (
        "| Regime | Horizon | Samples | Uncert MAE | Corr MAE | Delta | Uncert Cov | Corr Cov |"
    )
    for regime, block in report["by_regime"].items():
        lines.extend([regime_header, "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |"])
        for horizon, horizon_block in block["horizons"].items():
            unc = horizon_block["policies"]["uncertainty_aware"]
            corr = horizon_block["policies"]["correlation_aware"]
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(regime),
                        str(horizon),
                        str(unc["samples"]),
                        _fmt(unc["mae_pct"]),
                        _fmt(corr["mae_pct"]),
                        _fmt(horizon_block["uncertainty_minus_correlation_mae_pct"]),
                        _fmt(unc["coverage"]),
                        _fmt(corr["coverage"]),
                    ]
                )
                + " |"
            )
    lines.extend(["", "## Reproducibility", ""])
    lines.append(f"- Audit `sha256`: `{report['reproducibility']['audit_sha256']}`")
    formula = report["weighting_formula"]
    lines.append(f"- Weight formula version: `{formula['version']}`")
    lines.append(f"- Evidence policy: `{formula['evidence_policy']}`")
    lines.append(
        f"- Leakage guard: matured outcomes only (`{report['leakage_guard']['origin_cutoff']}`)"
    )
    lines.extend([""])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate uncertainty-aware ensemble weighting against the "
        "adaptive/correlation-aware baselines (#131)"
    )
    parser.add_argument("--samples-json", type=Path, required=True)
    parser.add_argument("--actuals-json", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("uncertainty_weighting_report.json"))
    parser.add_argument("--markdown", type=Path, default=Path("uncertainty_weighting_report.md"))
    parser.add_argument("--history-limit", type=int, default=200)
    parser.add_argument("--registry-db", type=Path)
    parser.add_argument("--decision", choices=("candidate", "inconclusive"), default="candidate")
    parser.add_argument("--git-sha", default=None)
    args = parser.parse_args()

    samples = json.loads(args.samples_json.read_text(encoding="utf-8"))
    raw_actuals = json.loads(args.actuals_json.read_text(encoding="utf-8"))
    if not isinstance(samples, list) or not isinstance(raw_actuals, dict):
        raise ValueError("samples must be a list and actuals must be an object")
    actuals = {int(key): float(value) for key, value in raw_actuals.items()}
    report = evaluate_uncertainty_weighting(
        [item for item in samples if isinstance(item, dict)],
        actuals,
        history_limit=args.history_limit,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(render_markdown(report), encoding="utf-8")

    summary: dict[str, Any] = {
        "generated_at": report["generated_at"],
        "samples": report["samples"],
        "json": str(args.output),
        "markdown": str(args.markdown),
    }
    if args.registry_db is not None:
        registry = ExperimentRegistry(args.registry_db)
        manifest, record, created = register_from_report(
            report, registry, git_sha=args.git_sha, decision=args.decision
        )
        summary["registered_run_id"] = record["run_id"]
        summary["registered"] = created
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
