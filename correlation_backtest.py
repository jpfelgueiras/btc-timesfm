#!/usr/bin/env python3
"""Walk-forward comparison of current and correlation-aware ensemble policies.

The evaluator consumes frozen model predictions, so both policies see identical
predictions and actuals. At each origin it exposes only target outcomes whose
timestamp is no later than that origin, preserving the production no-lookahead
contract while making the weighting-policy comparison cheap and reproducible.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from adaptive_weighting import adaptive_model_weights
from correlation_weighting import correlation_aware_model_weights
from forecast_engine import TARGET_HOURS


def _weighted_ensemble_price(
    current: float,
    model_predictions: dict[str, dict[str, dict[str, float]]],
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


def _aggregate(scores: list[dict[str, float]]) -> dict[str, float | int | None]:
    if not scores:
        return {"samples": 0, "mae_pct": None, "direction_accuracy": None}
    return {
        "samples": len(scores),
        "mae_pct": round(float(np.mean([item["absolute_error_pct"] for item in scores])), 6),
        "direction_accuracy": round(
            float(np.mean([item["direction_correct"] for item in scores])), 6
        ),
    }


def _history_snapshot(sample: dict[str, Any]) -> dict[str, Any]:
    forecast = sample["forecast"]
    return {
        "latest_close_at": forecast["latest_close_at"],
        "latest_close_usd": forecast["latest_close_usd"],
        "regime": forecast["regime"],
        "model_predictions": forecast["model_predictions"],
        "predictions": forecast.get("predictions", {}),
        "model_weights": forecast.get("model_weights", {}),
        "_outcomes": forecast.get("_outcomes", {}),
    }


def compare_frozen_samples(
    samples: list[dict[str, Any]],
    actual_by_timestamp: dict[int, float],
    *,
    history_limit: int = 200,
) -> dict[str, Any]:
    """Replay both weighting policies sequentially on identical frozen origins."""
    current_scores: dict[str, list[dict[str, float]]] = {
        f"{hour}h": [] for hour in TARGET_HOURS
    }
    correlation_scores: dict[str, list[dict[str, float]]] = {
        f"{hour}h": [] for hour in TARGET_HOURS
    }
    history: list[dict[str, Any]] = []
    policy_modes: dict[str, list[str]] = {f"{hour}h": [] for hour in TARGET_HOURS}

    ordered = sorted(samples, key=lambda item: int(item["origin_timestamp"]))
    for sample in ordered:
        timestamp = int(sample["origin_timestamp"])
        current = float(sample["current_price"])
        forecast = sample["forecast"]
        regime = str(forecast["regime"])
        model_predictions = forecast["model_predictions"]
        model_names = list(model_predictions)
        visible_actuals = {
            target: value for target, value in actual_by_timestamp.items() if target <= timestamp
        }

        for hour in TARGET_HOURS:
            horizon = f"{hour}h"
            actual = float(sample["actuals"][horizon])
            current_weights, _ = adaptive_model_weights(
                model_names,
                regime,
                hour,
                history,
                visible_actuals,
                history_limit=history_limit,
            )
            correlation_weights, diagnostics = correlation_aware_model_weights(
                model_names,
                regime,
                hour,
                history,
                visible_actuals,
                history_limit=history_limit,
            )
            current_price = _weighted_ensemble_price(
                current, model_predictions, horizon, current_weights
            )
            correlation_price = _weighted_ensemble_price(
                current, model_predictions, horizon, correlation_weights
            )
            current_scores[horizon].append(_score(current, current_price, actual))
            correlation_scores[horizon].append(_score(current, correlation_price, actual))
            policy_modes[horizon].append(str(diagnostics.get("correlation_mode")))

        history.append(_history_snapshot(sample))
        history = history[-history_limit:]

    by_horizon: dict[str, Any] = {}
    for horizon in current_scores:
        current_metrics = _aggregate(current_scores[horizon])
        correlation_metrics = _aggregate(correlation_scores[horizon])
        current_mae = current_metrics["mae_pct"]
        correlation_mae = correlation_metrics["mae_pct"]
        by_horizon[horizon] = {
            "current_adaptive": current_metrics,
            "correlation_aware": correlation_metrics,
            "correlation_minus_current_mae_pct": (
                round(float(correlation_mae) - float(current_mae), 6)
                if correlation_mae is not None and current_mae is not None
                else None
            ),
            "active_correlation_origins": sum(mode == "active" for mode in policy_modes[horizon]),
        }

    deltas = [
        float(item["correlation_minus_current_mae_pct"])
        for item in by_horizon.values()
        if item["correlation_minus_current_mae_pct"] is not None
    ]
    return {
        "samples": len(ordered),
        "history_limit": history_limit,
        "by_horizon": by_horizon,
        "mean_mae_delta_pct": round(float(np.mean(deltas)), 6) if deltas else None,
        "interpretation": "negative MAE delta favors correlation-aware weighting",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare adaptive and correlation-aware weighting on frozen samples"
    )
    parser.add_argument("--samples-json", type=Path, required=True)
    parser.add_argument("--actuals-json", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("correlation_backtest_report.json"))
    args = parser.parse_args()

    samples = json.loads(args.samples_json.read_text(encoding="utf-8"))
    raw_actuals = json.loads(args.actuals_json.read_text(encoding="utf-8"))
    if not isinstance(samples, list) or not isinstance(raw_actuals, dict):
        raise ValueError("samples must be a list and actuals must be an object")
    actuals = {int(key): float(value) for key, value in raw_actuals.items()}
    report = compare_frozen_samples([item for item in samples if isinstance(item, dict)], actuals)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
