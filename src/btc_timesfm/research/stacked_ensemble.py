#!/usr/bin/env python3
"""Leakage-safe stacking evaluation over horizon and regime specialists."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from btc_timesfm.forecasting.correlation_weighting import correlation_aware_model_weights
from btc_timesfm.forecasting.cross_validation import (
    DEFAULT_CV_FOLDS,
    DEFAULT_MIN_TRAIN_SAMPLES,
    assert_no_fold_leakage,
    build_purged_walk_forward_folds,
    fold_definition,
)
from btc_timesfm.forecasting.forecast_engine import TARGET_HOURS
from btc_timesfm.forecasting.statistical_significance import (
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_MIN_PAIRED_SAMPLES,
    DEFAULT_SEED,
    paired_bootstrap_comparison,
)
from btc_timesfm.research.regime_specialists import (
    UNKNOWN_REGIME,
    _detect_regime,
    _snapshot,
    choose_expert,
    expert_model_weights,
)

STACKING_VERSION = 1
SPECIALISTS = ("production", "horizon", "regime")
DEFAULT_HISTORY_LIMIT = 200
DEFAULT_LOW_SAMPLE_THRESHOLD = DEFAULT_MIN_PAIRED_SAMPLES


def _ensemble_price(current: float, forecasts: dict[str, dict[str, Any]], weights: dict[str, float], horizon: str) -> float:
    active = [(name, weight) for name, weight in weights.items() if weight > 0 and name in forecasts]
    if not active:
        raise ValueError("specialist has no active model weights")
    total = sum(weight for _, weight in active)
    change = sum(weight * math.log(float(forecasts[name][horizon]["price_usd"]) / current) for name, weight in active)
    return current * math.exp(change / total)


def _horizon_weights(names: list[str], regime: str, hour: int, history: list[dict[str, Any]], actuals: dict[int, float]) -> dict[str, float]:
    limit = {2: 80, 4: 120, 8: 160, 16: 240}[hour]
    weights, _ = correlation_aware_model_weights(names, regime, hour, history, actuals, history_limit=limit)
    return weights


def _specialist_predictions(sample: dict[str, Any], history: list[dict[str, Any]], actuals: dict[int, float]) -> tuple[dict[str, dict[str, float]], str]:
    forecast = sample["forecast"]
    current = float(sample["current_price"])
    models = forecast["model_predictions"]
    names = list(models)
    regime = _detect_regime(dict(forecast.get("market_features") or {}))
    baseline_regime = regime or str(forecast.get("regime") or "range")
    output = {name: {} for name in SPECIALISTS}
    sparse = False
    for hour in TARGET_HOURS:
        horizon = f"{hour}h"
        production, diagnostics = correlation_aware_model_weights(names, baseline_regime, hour, history, actuals, history_limit=DEFAULT_HISTORY_LIMIT)
        sparse = sparse or diagnostics.get("source") == "insufficient_history"
        output["production"][horizon] = _ensemble_price(current, models, production, horizon)
        output["horizon"][horizon] = _ensemble_price(current, models, _horizon_weights(names, baseline_regime, hour, history, actuals), horizon)
    expert = choose_expert(regime, sparse)
    for hour in TARGET_HOURS:
        horizon = f"{hour}h"
        weights, _ = expert_model_weights(expert, names, regime, hour, history, actuals)
        output["regime"][horizon] = _ensemble_price(current, models, weights, horizon)
    return output, regime or UNKNOWN_REGIME


def _fit_weights(features: np.ndarray, targets: np.ndarray) -> np.ndarray:
    """Fit non-negative simplex weights with deterministic projected gradient descent."""
    if len(features) == 0:
        return np.full(len(SPECIALISTS), 1.0 / len(SPECIALISTS))
    x = features - 1.0
    y = targets - 1.0
    ridge = 1e-3
    matrix = x.T @ x + ridge * np.eye(x.shape[1])
    vector = x.T @ y
    weights = np.full(x.shape[1], 1.0 / x.shape[1])
    step = 1.0 / max(float(np.linalg.norm(matrix, ord=2)), 1.0)
    for _ in range(400):
        weights -= step * (matrix @ weights - vector)
        weights = np.maximum(weights, 0.0)
        total = float(weights.sum())
        weights = weights / total if total else np.full_like(weights, 1.0 / len(weights))
    return weights


def _score(current: float, predicted: float, actual: float) -> dict[str, float]:
    return {"absolute_error_pct": abs(predicted - actual) / actual * 100.0, "direction_correct": float((predicted - current) * (actual - current) >= 0.0)}


def _metrics(scores: list[dict[str, float]]) -> dict[str, float | int | None]:
    if not scores:
        return {"samples": 0, "mae_pct": None, "direction_accuracy": None}
    return {"samples": len(scores), "mae_pct": round(float(np.mean([x["absolute_error_pct"] for x in scores])), 6), "direction_accuracy": round(float(np.mean([x["direction_correct"] for x in scores])), 6)}


def _comparison(candidate: list[dict[str, float]], baseline: list[dict[str, float]], iterations: int, minimum: int) -> dict[str, Any]:
    return paired_bootstrap_comparison([x["absolute_error_pct"] for x in candidate], [x["absolute_error_pct"] for x in baseline], metric="mae_pct", lower_is_better=True, iterations=iterations, min_samples=minimum, seed=DEFAULT_SEED)


def build_experiment_manifest(report: dict[str, Any], *, data_id: str = "durable-history") -> dict[str, Any]:
    """Return a deterministic registry-compatible manifest for a stack evaluation."""
    configuration = report["configuration"]
    digest = hashlib.sha256(json.dumps(configuration, sort_keys=True).encode("utf-8")).hexdigest()
    return {
        "manifest_version": 1,
        "run_id": f"stacked-ensemble-{digest[:16]}",
        "run_type": "stacked_ensemble",
        "configuration_id": f"cfg-{digest[:20]}",
        "data_id": data_id,
        "configuration": {"stacking": configuration, "specialists": report["specialists"]},
        "metrics": {"by_horizon": report["by_horizon"], "by_regime": report["by_regime"]},
        "decision": "candidate",
        "promotion_mode": "shadow_only",
    }


def evaluate_stacked_ensemble(samples: list[dict[str, Any]], actual_by_timestamp: dict[int, float], *, folds: int = DEFAULT_CV_FOLDS, min_train_samples: int = DEFAULT_MIN_TRAIN_SAMPLES, low_sample_threshold: int = DEFAULT_LOW_SAMPLE_THRESHOLD, bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS, bootstrap_min_samples: int = DEFAULT_MIN_PAIRED_SAMPLES) -> dict[str, Any]:
    """Evaluate a stack whose meta-learner is fit solely on each purged fold's train origins."""
    ordered = sorted(samples, key=lambda item: int(item["origin_timestamp"]))
    origins = [int(item["origin_timestamp"]) for item in ordered]
    split = build_purged_walk_forward_folds(origins, folds=folds, min_train_samples=min_train_samples, max_target_hours=max(TARGET_HOURS))
    all_specialists: list[dict[str, dict[str, float]]] = []
    regimes: list[str] = []
    history: list[dict[str, Any]] = []
    for sample in ordered:
        timestamp = int(sample["origin_timestamp"])
        visible = {key: value for key, value in actual_by_timestamp.items() if key <= timestamp}
        predictions, regime = _specialist_predictions(sample, history, visible)
        all_specialists.append(predictions)
        regimes.append(regime)
        history.append(_snapshot(sample, regime))
    stacked = {f"{hour}h": [] for hour in TARGET_HOURS}
    production = {f"{hour}h": [] for hour in TARGET_HOURS}
    persistence = {f"{hour}h": [] for hour in TARGET_HOURS}
    by_regime: dict[str, dict[str, dict[str, list[dict[str, float]]]]] = {}
    fold_reports: list[dict[str, Any]] = []
    for fold in split:
        assert_no_fold_leakage(fold, origins, max_target_hours=max(TARGET_HOURS))
        report = fold_definition(fold, origins)
        report["weights"] = {}
        for hour in TARGET_HOURS:
            horizon = f"{hour}h"
            train_x: list[list[float]] = []
            train_y: list[float] = []
            for index in fold.train_indices:
                actual = ordered[index].get("actuals", {}).get(horizon)
                if actual is None:
                    continue
                current = float(ordered[index]["current_price"])
                train_x.append([all_specialists[index][name][horizon] / current for name in SPECIALISTS])
                train_y.append(float(actual) / current)
            weights = _fit_weights(np.asarray(train_x), np.asarray(train_y))
            report["weights"][horizon] = {name: round(float(weights[pos]), 6) for pos, name in enumerate(SPECIALISTS)}
            for index in fold.validation_indices:
                actual = ordered[index].get("actuals", {}).get(horizon)
                if actual is None:
                    continue
                current = float(ordered[index]["current_price"])
                values = np.asarray([all_specialists[index][name][horizon] for name in SPECIALISTS])
                candidate = float(values @ weights)
                actual_value = float(actual)
                candidate_score = _score(current, candidate, actual_value)
                production_score = _score(current, all_specialists[index]["production"][horizon], actual_value)
                persistence_price = float(ordered[index]["forecast"]["model_predictions"]["persistence"][horizon]["price_usd"])
                persistence_score = _score(current, persistence_price, actual_value)
                stacked[horizon].append(candidate_score)
                production[horizon].append(production_score)
                persistence[horizon].append(persistence_score)
                bucket = regimes[index]
                segment = by_regime.setdefault(bucket, {}).setdefault(horizon, {"stack": [], "production": [], "persistence": []})
                segment["stack"].append(candidate_score)
                segment["production"].append(production_score)
                segment["persistence"].append(persistence_score)
        fold_reports.append(report)
    def segment(candidate: list[dict[str, float]], baseline: list[dict[str, float]]) -> dict[str, Any]:
        comparison = _comparison(candidate, baseline, bootstrap_iterations, bootstrap_min_samples)
        return {"stack": _metrics(candidate), "baseline": _metrics(baseline), "comparison": comparison, "low_sample": len(candidate) < low_sample_threshold, "inconclusive": len(candidate) < low_sample_threshold or comparison["conclusion"] == "inconclusive"}
    report = {"stacking_version": STACKING_VERSION, "specialists": list(SPECIALISTS), "samples": len(ordered), "folds": fold_reports, "configuration": {"folds": folds, "min_train_samples": min_train_samples, "purge_hours": max(TARGET_HOURS), "meta_learner": "nonnegative_simplex_ridge", "low_sample_threshold": low_sample_threshold}, "by_horizon": {horizon: {"vs_production": segment(stacked[horizon], production[horizon]), "vs_persistence": segment(stacked[horizon], persistence[horizon])} for horizon in stacked}, "by_regime": {regime: {horizon: {"vs_production": segment(values["stack"], values["production"]), "vs_persistence": segment(values["stack"], values["persistence"])} for horizon, values in horizons.items()} for regime, horizons in by_regime.items()}, "regime_samples": dict(Counter(regimes))}
    report["experiment_manifest"] = build_experiment_manifest(report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate a purged walk-forward stacked ensemble")
    parser.add_argument("--samples-json", type=Path, required=True)
    parser.add_argument("--actuals-json", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("stacked_ensemble_report.json"))
    args = parser.parse_args()
    samples = json.loads(args.samples_json.read_text(encoding="utf-8"))
    actuals = {int(key): float(value) for key, value in json.loads(args.actuals_json.read_text(encoding="utf-8")).items()}
    report = evaluate_stacked_ensemble(samples, actuals)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
