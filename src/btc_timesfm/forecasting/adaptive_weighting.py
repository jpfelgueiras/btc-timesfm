"""Adaptive performance-based ensemble weighting.

This module keeps the weighting policy separate from the forecasting engine so it
can consume durable matured outcomes without coupling TimesFM inference to the
SQLite storage implementation. Production attaches persisted outcomes to the
reconstructed snapshots; walk-forward backtests fall back to candles that are
known at each simulated forecast origin.
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import Any, Iterable

import numpy as np

from btc_timesfm.forecasting.forecast_engine import (
    ADAPTIVE_DIRECTION_REWARD,
    ADAPTIVE_FULL_SAMPLES,
    ADAPTIVE_MAE_LAMBDA,
    ADAPTIVE_MAX_BLEND,
    ADAPTIVE_MAX_WEIGHT,
    ADAPTIVE_MIN_SAMPLES,
    ADAPTIVE_MIN_WEIGHT,
    PERSISTENCE_FALLBACK_BOOST,
    _bounded_normalize,
    static_model_weights,
)

DEFAULT_HISTORY_LIMIT = max(
    ADAPTIVE_MIN_SAMPLES, int(os.getenv("BTC_ADAPTIVE_HISTORY_LIMIT", "200"))
)
TARGET_INTERVAL_COVERAGE = 0.80
COVERAGE_PENALTY = 0.35
MIN_COVERAGE_SAMPLES = 3


def _metric_float(metric: dict[str, Any], key: str) -> float:
    value = metric.get(key)
    return float(value) if value is not None else 0.0


def _direction(value: float, epsilon: float = 1e-12) -> int:
    return 1 if value > epsilon else -1 if value < -epsilon else 0


def attach_persisted_outcomes(
    snapshots: list[dict[str, Any]],
    rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach matured SQLite outcome rows to reconstructed forecast snapshots.

    The returned list reuses the snapshot dictionaries and adds a private
    ``_outcomes`` mapping. Persisted outcomes are authoritative and allow
    adaptive weighting to use history older than Kraken's recent OHLC window.
    """
    by_origin = {
        str(snapshot.get("latest_close_at")): snapshot
        for snapshot in snapshots
        if snapshot.get("latest_close_at")
    }
    for row in rows:
        actual = row.get("actual_target_price_usd")
        if actual is None:
            continue
        snapshot = by_origin.get(str(row.get("origin_at")))
        if snapshot is None:
            continue
        model_name = str(row.get("model_name"))
        if model_name == "ensemble":
            continue
        try:
            hour = int(row["horizon_hours"])
        except (KeyError, TypeError, ValueError):
            continue
        horizon = f"{hour}h"
        snapshot.setdefault("_outcomes", {}).setdefault(horizon, {})[model_name] = {
            "actual_target_price_usd": float(actual),
            "absolute_error_pct": row.get("absolute_error_pct"),
            "signed_error_pct": row.get("signed_error_pct"),
            "direction_correct": row.get("direction_correct"),
            "within_q10_q90": row.get("within_q10_q90"),
            "matured_at": row.get("matured_at"),
        }
    return snapshots


def _persisted_actual(snapshot: dict[str, Any], horizon: str, model_name: str) -> float | None:
    try:
        value = snapshot["_outcomes"][horizon][model_name]["actual_target_price_usd"]
    except (KeyError, TypeError):
        return None
    try:
        actual = float(value)
    except (TypeError, ValueError):
        return None
    return actual if actual > 0 else None


def _score_history_for_model(
    history: list[dict[str, Any]],
    actual_by_timestamp: dict[int, float],
    model_name: str,
    hour: int,
    regime: str | None,
    history_limit: int,
) -> list[dict[str, float | bool | None | str]]:
    horizon = f"{hour}h"
    scores: list[dict[str, float | bool | None | str]] = []

    # Newest samples win. The limit applies after horizon/regime filtering, so a
    # rare regime can use up to the configured number of relevant observations.
    for snapshot in reversed(history):
        if len(scores) >= history_limit:
            break
        if regime is not None and snapshot.get("regime") != regime:
            continue
        try:
            origin = datetime.fromisoformat(str(snapshot["latest_close_at"]).replace("Z", "+00:00"))
            if origin.tzinfo is None:
                origin = origin.replace(tzinfo=timezone.utc)
            origin = origin.astimezone(timezone.utc)
            previous_close = float(snapshot["latest_close_usd"])
            item = snapshot["model_predictions"][model_name][horizon]
            predicted = float(item["price_usd"])
        except (KeyError, TypeError, ValueError):
            continue

        actual = _persisted_actual(snapshot, horizon, model_name)
        outcome_source = "durable"
        if actual is None:
            target = int(origin.timestamp()) + hour * 3600
            candle_actual = actual_by_timestamp.get(target)
            if candle_actual is None or float(candle_actual) <= 0:
                continue
            actual = float(candle_actual)
            outcome_source = "candle"

        error = predicted - actual
        q10 = item.get("q10_usd") if isinstance(item, dict) else None
        q90 = item.get("q90_usd") if isinstance(item, dict) else None
        within: bool | None = None
        if q10 is not None and q90 is not None:
            try:
                within = float(q10) <= actual <= float(q90)
            except (TypeError, ValueError):
                within = None

        scores.append(
            {
                "absolute_error_pct": abs(error) / actual * 100.0,
                "signed_error_pct": error / actual * 100.0,
                "direction_correct": _direction(predicted - previous_close)
                == _direction(actual - previous_close),
                "within_q10_q90": within,
                "outcome_source": outcome_source,
            }
        )

    # Metric aggregation is order-independent; restore chronological ordering for
    # deterministic diagnostics and easier debugging.
    scores.reverse()
    return scores


def _performance_metrics(scores: list[dict[str, Any]]) -> dict[str, float | int | None]:
    covered = [
        bool(score["within_q10_q90"]) for score in scores if score.get("within_q10_q90") is not None
    ]
    durable_samples = sum(score.get("outcome_source") == "durable" for score in scores)
    return {
        "samples": len(scores),
        "mae_pct": float(np.mean([float(s["absolute_error_pct"]) for s in scores])),
        "mean_signed_error_pct": float(np.mean([float(s["signed_error_pct"]) for s in scores])),
        "direction_accuracy": float(np.mean([bool(s["direction_correct"]) for s in scores])),
        "q10_q90_coverage": float(np.mean(covered)) if covered else None,
        "interval_samples": len(covered),
        "durable_outcome_samples": durable_samples,
        "candle_outcome_samples": len(scores) - durable_samples,
    }


def adaptive_model_weights(
    model_names: list[str],
    regime: str,
    hour: int,
    history: list[dict[str, Any]],
    actual_by_timestamp: dict[int, float],
    enabled: bool = True,
    history_limit: int | None = None,
    confidence: float = 1.0,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Blend static priors with recent out-of-sample performance.

    Weighting is independent per horizon. Current-regime samples are preferred;
    all regimes are used when the current regime is sparse. Learned weights are
    shrunk toward the hand-tuned prior, bounded, and biased back toward
    persistence when the complex models fail to beat it.
    """
    limit = max(ADAPTIVE_MIN_SAMPLES, int(history_limit or DEFAULT_HISTORY_LIMIT))
    adaptive_confidence = min(1.0, max(0.0, float(confidence)))
    prior = static_model_weights(model_names, regime)

    regime_scores = {
        name: _score_history_for_model(history, actual_by_timestamp, name, hour, regime, limit)
        for name in model_names
    }
    all_scores = {
        name: _score_history_for_model(history, actual_by_timestamp, name, hour, None, limit)
        for name in model_names
    }

    min_regime_samples = min((len(scores) for scores in regime_scores.values()), default=0)
    min_all_samples = min((len(scores) for scores in all_scores.values()), default=0)

    if not enabled:
        source = "disabled"
        selected = all_scores
    elif min_regime_samples >= ADAPTIVE_MIN_SAMPLES:
        source = "regime"
        selected = regime_scores
    elif min_all_samples >= ADAPTIVE_MIN_SAMPLES:
        source = "all_regimes"
        selected = all_scores
    else:
        source = "insufficient_history"
        selected = all_scores

    if enabled and adaptive_confidence <= 0.0 and source != "insufficient_history":
        source = "drift_fallback"

    metrics = {
        name: _performance_metrics(scores)
        if scores
        else {
            "samples": 0,
            "mae_pct": None,
            "mean_signed_error_pct": None,
            "direction_accuracy": None,
            "q10_q90_coverage": None,
            "interval_samples": 0,
            "durable_outcome_samples": 0,
            "candle_outcome_samples": 0,
        }
        for name, scores in selected.items()
    }

    if not enabled or source in {"insufficient_history", "drift_fallback"}:
        diagnostics_static = {
            "mode": "static_prior",
            "source": source,
            "horizon": f"{hour}h",
            "regime": regime,
            "history_limit": limit,
            "adaptive_confidence": round(adaptive_confidence, 4),
            "blend_factor": 0.0,
            "sample_count": min_all_samples if source != "regime" else min_regime_samples,
            "persistence_mae_pct": metrics.get("persistence", {}).get("mae_pct"),
            "models": {
                name: {
                    **metric,
                    "prior_weight": round(prior[name], 6),
                    "raw_score": None,
                    "adaptive_weight": None,
                    "final_weight": round(prior[name], 6),
                    "edge_vs_persistence_mae_pct": None,
                }
                for name, metric in metrics.items()
            },
        }
        return prior, diagnostics_static

    persistence_mae = (
        _metric_float(metrics["persistence"], "mae_pct")
        if "persistence" in metrics and metrics["persistence"].get("mae_pct") is not None
        else None
    )
    raw_scores: dict[str, float] = {}
    for name, metric in metrics.items():
        mae = _metric_float(metric, "mae_pct")
        direction_accuracy = _metric_float(metric, "direction_accuracy")
        bias = abs(_metric_float(metric, "mean_signed_error_pct"))

        score = math.exp(-ADAPTIVE_MAE_LAMBDA * mae)
        score *= 1.0 + ADAPTIVE_DIRECTION_REWARD * (direction_accuracy - 0.5) * 2.0
        score *= math.exp(-0.35 * bias)

        coverage = metric.get("q10_q90_coverage")
        if (
            coverage is not None
            and int(metric.get("interval_samples") or 0) >= MIN_COVERAGE_SAMPLES
        ):
            score *= math.exp(-COVERAGE_PENALTY * abs(float(coverage) - TARGET_INTERVAL_COVERAGE))

        if persistence_mae is not None and name != "persistence":
            edge = persistence_mae - mae
            if edge < 0:
                score *= math.exp(1.5 * edge)

        raw_scores[name] = max(score, 1e-9)

    raw_total = sum(raw_scores.values())
    adaptive = {name: score / raw_total for name, score in raw_scores.items()}

    sample_count = min(int(_metric_float(metric, "samples")) for metric in metrics.values())
    progress = min(
        1.0,
        max(
            0.0,
            (sample_count - ADAPTIVE_MIN_SAMPLES)
            / max(1, ADAPTIVE_FULL_SAMPLES - ADAPTIVE_MIN_SAMPLES),
        ),
    )
    unscaled_blend = 0.25 + (ADAPTIVE_MAX_BLEND - 0.25) * progress
    blend = unscaled_blend * adaptive_confidence
    blended = {name: (1.0 - blend) * prior[name] + blend * adaptive[name] for name in model_names}

    complex_maes = [
        _metric_float(metrics[name], "mae_pct")
        for name in model_names
        if name != "persistence" and metrics[name]["mae_pct"] is not None
    ]
    persistence_fallback = False
    if (
        persistence_mae is not None
        and complex_maes
        and float(np.mean(complex_maes)) >= persistence_mae
        and "persistence" in blended
    ):
        blended["persistence"] += PERSISTENCE_FALLBACK_BOOST
        persistence_fallback = True

    final = _bounded_normalize(blended, ADAPTIVE_MIN_WEIGHT, ADAPTIVE_MAX_WEIGHT)
    model_diagnostics: dict[str, Any] = {}
    diagnostics: dict[str, Any] = {
        "mode": "adaptive",
        "source": source,
        "horizon": f"{hour}h",
        "regime": regime,
        "history_limit": limit,
        "adaptive_confidence": round(adaptive_confidence, 4),
        "unscaled_blend_factor": round(unscaled_blend, 4),
        "blend_factor": round(blend, 4),
        "sample_count": sample_count,
        "persistence_fallback": persistence_fallback,
        "persistence_mae_pct": round(persistence_mae, 6) if persistence_mae is not None else None,
        "models": model_diagnostics,
    }
    for name, metric in metrics.items():
        mae = _metric_float(metric, "mae_pct")
        model_diagnostics[name] = {
            **{
                key: round(value, 6) if isinstance(value, float) else value
                for key, value in metric.items()
            },
            "prior_weight": round(prior[name], 6),
            "raw_score": round(raw_scores[name], 8),
            "adaptive_weight": round(adaptive[name], 6),
            "final_weight": round(final[name], 6),
            "edge_vs_persistence_mae_pct": (
                round(persistence_mae - mae, 6) if persistence_mae is not None else None
            ),
        }

    return final, diagnostics
