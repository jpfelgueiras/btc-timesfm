#!/usr/bin/env python3
"""No-lookahead comparison of legacy and validated BTC regime detectors.

Frozen model predictions are replayed chronologically. Each detector maintains
its own history labels because adaptive weighting filters historical performance
by regime. Only actual target candles observable by the current origin are
passed to the weighting policy.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from btc_timesfm.forecasting.correlation_weighting import correlation_aware_model_weights
from btc_timesfm.forecasting.forecast_engine import TARGET_HOURS
from btc_timesfm.data.regime_detection import (
    compare_regime_methods,
    heuristic_regime,
    transition_churn,
    validated_regime,
)

DEFAULT_MAX_HORIZON_REGRESSION_PCT = 5.0


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


def _snapshot(sample: dict[str, Any], regime: str) -> dict[str, Any]:
    forecast = sample["forecast"]
    return {
        "latest_close_at": forecast["latest_close_at"],
        "latest_close_usd": forecast["latest_close_usd"],
        "regime": regime,
        "market_features": forecast.get("market_features", {}),
        "model_predictions": forecast["model_predictions"],
        "predictions": forecast.get("predictions", {}),
        "model_weights": forecast.get("model_weights", {}),
        "_outcomes": forecast.get("_outcomes", {}),
    }


def compare_detectors_out_of_sample(
    samples: list[dict[str, Any]],
    actual_by_timestamp: dict[int, float],
    *,
    history_limit: int = 200,
    max_horizon_regression_pct: float = DEFAULT_MAX_HORIZON_REGRESSION_PCT,
) -> dict[str, Any]:
    """Replay old/new regime labels through the same adaptive ensemble policy."""
    if max_horizon_regression_pct < 0:
        raise ValueError("max_horizon_regression_pct must be non-negative")
    ordered = sorted(samples, key=lambda item: int(item["origin_timestamp"]))
    histories: dict[str, list[dict[str, Any]]] = {"heuristic": [], "validated": []}
    labels: dict[str, list[str]] = {"heuristic": [], "validated": []}
    scores: dict[str, dict[str, list[dict[str, float]]]] = {
        policy: {f"{hour}h": [] for hour in TARGET_HOURS} for policy in histories
    }
    scores_by_regime: dict[str, dict[str, dict[str, list[dict[str, float]]]]] = {
        policy: {
            regime: {f"{hour}h": [] for hour in TARGET_HOURS}
            for regime in ("range", "trending", "high_volatility")
        }
        for policy in histories
    }
    feature_rows: list[dict[str, Any]] = []

    for sample in ordered:
        timestamp = int(sample["origin_timestamp"])
        current = float(sample["current_price"])
        forecast = sample["forecast"]
        features = dict(forecast.get("market_features") or {})
        feature_rows.append(features)
        policy_labels = {
            "heuristic": heuristic_regime(features),
            "validated": validated_regime(features),
        }
        visible_actuals = {
            target: value for target, value in actual_by_timestamp.items() if target <= timestamp
        }
        model_predictions = forecast["model_predictions"]
        model_names = list(model_predictions)

        for policy, regime in policy_labels.items():
            labels[policy].append(regime)
            for hour in TARGET_HOURS:
                horizon = f"{hour}h"
                weights, _ = correlation_aware_model_weights(
                    model_names,
                    regime,
                    hour,
                    histories[policy],
                    visible_actuals,
                    history_limit=history_limit,
                )
                predicted = _weighted_ensemble_price(current, model_predictions, horizon, weights)
                item = _score(current, predicted, float(sample["actuals"][horizon]))
                scores[policy][horizon].append(item)
                scores_by_regime[policy][regime][horizon].append(item)

            histories[policy].append(_snapshot(sample, regime))
            histories[policy] = histories[policy][-history_limit:]

    by_horizon: dict[str, Any] = {}
    regression_vetoes: list[str] = []
    for hour in TARGET_HOURS:
        horizon = f"{hour}h"
        old_metrics = _aggregate(scores["heuristic"][horizon])
        new_metrics = _aggregate(scores["validated"][horizon])
        old_mae = old_metrics["mae_pct"]
        new_mae = new_metrics["mae_pct"]
        relative_regression = None
        if old_mae is not None and new_mae is not None and float(old_mae) > 0:
            relative_regression = (float(new_mae) / float(old_mae) - 1.0) * 100.0
            if relative_regression > max_horizon_regression_pct:
                regression_vetoes.append(horizon)
        by_horizon[horizon] = {
            "heuristic": old_metrics,
            "validated": new_metrics,
            "validated_minus_heuristic_mae_pct": (
                round(float(new_mae) - float(old_mae), 6)
                if old_mae is not None and new_mae is not None
                else None
            ),
            "relative_mae_regression_pct": (
                round(relative_regression, 4) if relative_regression is not None else None
            ),
        }

    by_regime = {
        policy: {
            regime: {
                horizon: _aggregate(regime_scores)
                for horizon, regime_scores in horizon_scores.items()
            }
            for regime, horizon_scores in policy_regimes.items()
        }
        for policy, policy_regimes in scores_by_regime.items()
    }

    return {
        "samples": len(ordered),
        "history_limit": history_limit,
        "detector_methods": compare_regime_methods(feature_rows),
        "transition_churn": {
            policy: transition_churn(policy_labels) for policy, policy_labels in labels.items()
        },
        "label_sequences": labels,
        "by_horizon": by_horizon,
        "by_regime": by_regime,
        "safety_gate": {
            "max_allowed_horizon_regression_pct": max_horizon_regression_pct,
            "regression_veto_horizons": regression_vetoes,
            "passes": not regression_vetoes,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare regime detectors on frozen forecasts")
    parser.add_argument("--samples-json", type=Path, required=True)
    parser.add_argument("--actuals-json", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("regime_backtest_report.json"))
    args = parser.parse_args()

    samples = json.loads(args.samples_json.read_text(encoding="utf-8"))
    raw_actuals = json.loads(args.actuals_json.read_text(encoding="utf-8"))
    if not isinstance(samples, list) or not isinstance(raw_actuals, dict):
        raise ValueError("samples must be a list and actuals must be an object")
    actuals = {int(key): float(value) for key, value in raw_actuals.items()}
    report = compare_detectors_out_of_sample(
        [item for item in samples if isinstance(item, dict)], actuals
    )
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
