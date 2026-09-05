#!/usr/bin/env python3
"""Walk-forward backtest for the BTC forecast ensemble.

Historical candles come from Binance BTCUSDT because Kraken's OHLC endpoint only
exposes a limited recent window. BTCUSDT is used as a liquid USD proxy for
research/backtesting; production forecasts continue to use Kraken BTC/USD.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import requests

import forecast_engine
from adaptive_weighting import DEFAULT_HISTORY_LIMIT, adaptive_model_weights
from benchmarks import BENCHMARK_NAMES, benchmark_forecasts, benchmark_metadata
from cross_validation import (
    DEFAULT_CV_FOLDS,
    DEFAULT_EMBARGO_HOURS,
    DEFAULT_MIN_TRAIN_SAMPLES,
    DEFAULT_PURGE_HOURS,
    DEFAULT_ROLLING_TRAIN_SAMPLES,
    WalkForwardFold,
    assert_no_fold_leakage,
    build_purged_walk_forward_folds,
    fold_definition,
)
from experiment_manifest import build_experiment_manifest, seed_everything
from forecast_engine import (
    MarketData,
    TARGET_HOURS,
    build_forecast,
    load_timesfm,
    static_model_weights,
)


BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
REPORT_PATH = Path("backtest_report.json")
HORIZONS = ("2h", "4h", "8h", "16h")

# Use the same issue #6 adaptive policy as production. During walk-forward tests
# there is no durable DB; only target candles already visible at each simulated
# origin can mature prior forecasts, which preserves no-look-ahead behavior.
forecast_engine.adaptive_model_weights = adaptive_model_weights


def fetch_binance_history(days: int) -> MarketData:
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    rows: list[list[Any]] = []
    cursor = start_ms

    while cursor < end_ms:
        response = requests.get(
            BINANCE_KLINES,
            params={
                "symbol": "BTCUSDT",
                "interval": "1h",
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1000,
            },
            timeout=30,
        )
        response.raise_for_status()
        batch = response.json()
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1][0]) + 3600_000
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        time.sleep(0.05)

    if len(rows) < 550:
        raise RuntimeError(f"Not enough historical candles: {len(rows)}")

    timestamps = [int(row[0] / 1000) + 3600 for row in rows]
    return MarketData(
        timestamps=timestamps,
        opens=np.asarray([float(row[1]) for row in rows], dtype=np.float32),
        highs=np.asarray([float(row[2]) for row in rows], dtype=np.float32),
        lows=np.asarray([float(row[3]) for row in rows], dtype=np.float32),
        closes=np.asarray([float(row[4]) for row in rows], dtype=np.float32),
        volumes=np.asarray([float(row[5]) for row in rows], dtype=np.float32),
    )


def slice_market(data: MarketData, end_index: int, context: int = 513) -> MarketData:
    start = max(0, end_index - context + 1)
    sl = slice(start, end_index + 1)
    return MarketData(
        timestamps=data.timestamps[start : end_index + 1],
        opens=data.opens[sl],
        highs=data.highs[sl],
        lows=data.lows[sl],
        closes=data.closes[sl],
        volumes=data.volumes[sl],
    )


def direction(value: float, epsilon: float = 1e-9) -> int:
    return 1 if value > epsilon else -1 if value < -epsilon else 0


def score_one(current: float, predicted: float, actual: float) -> dict[str, Any]:
    error = predicted - actual
    return {
        "absolute_error_pct": abs(error) / actual * 100.0,
        "signed_error_pct": error / actual * 100.0,
        "direction_correct": direction(predicted - current) == direction(actual - current),
    }


def _aggregate_scores(scores: list[dict[str, Any]]) -> dict[str, Any]:
    if not scores:
        return {
            "samples": 0,
            "mae_pct": None,
            "mean_signed_error_pct": None,
            "direction_accuracy": None,
        }
    return {
        "samples": len(scores),
        "mae_pct": round(float(np.mean([s["absolute_error_pct"] for s in scores])), 4),
        "mean_signed_error_pct": round(float(np.mean([s["signed_error_pct"] for s in scores])), 4),
        "direction_accuracy": round(float(np.mean([s["direction_correct"] for s in scores])), 4),
    }


def _dispersion(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"folds": 0, "mean": None, "std": None, "min": None, "max": None}
    array = np.asarray(values, dtype=np.float64)
    return {
        "folds": len(values),
        "mean": round(float(np.mean(array)), 6),
        "std": round(float(np.std(array, ddof=0)), 6),
        "min": round(float(np.min(array)), 6),
        "max": round(float(np.max(array)), 6),
    }


def weighted_ensemble_price(
    current: float,
    model_predictions: dict[str, Any],
    horizon: str,
    weights: dict[str, float],
) -> float:
    active = [
        (name, weight)
        for name, weight in weights.items()
        if weight > 0 and name in model_predictions
    ]
    if not active:
        return current
    total = sum(weight for _, weight in active)
    log_change = 0.0
    for name, weight in active:
        price = float(model_predictions[name][horizon]["price_usd"])
        log_change += weight / total * math.log(price / current)
    return current * math.exp(log_change)


def static_ensemble_price(forecast: dict[str, Any], horizon: str) -> float:
    current = float(forecast["latest_close_usd"])
    model_predictions = forecast["model_predictions"]
    weights = static_model_weights(list(model_predictions), forecast["regime"])
    return weighted_ensemble_price(current, model_predictions, horizon, weights)


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for horizon in HORIZONS:
        per_model: dict[str, list[dict[str, Any]]] = {}
        benchmark_scores: dict[str, list[dict[str, Any]]] = {}
        ensemble_coverage: list[bool] = []
        regime_scores: dict[str, list[dict[str, Any]]] = {}
        benchmark_regime_scores: dict[str, dict[str, list[dict[str, Any]]]] = {}

        for sample in samples:
            actual = sample["actuals"][horizon]
            current = sample["current_price"]
            regime = sample["forecast"]["regime"]
            ensemble = sample["forecast"]["predictions"][horizon]
            score = score_one(current, ensemble["price_usd"], actual)
            per_model.setdefault("adaptive_ensemble", []).append(score)
            ensemble_coverage.append(ensemble["q10_usd"] <= actual <= ensemble["q90_usd"])
            regime_scores.setdefault(regime, []).append(score)

            static_price = static_ensemble_price(sample["forecast"], horizon)
            per_model.setdefault("static_ensemble", []).append(
                score_one(current, static_price, actual)
            )

            for name, horizons in sample["forecast"]["model_predictions"].items():
                per_model.setdefault(name, []).append(
                    score_one(current, horizons[horizon]["price_usd"], actual)
                )

            regime_benchmarks = benchmark_regime_scores.setdefault(regime, {})
            for name, horizons in sample["benchmarks"].items():
                benchmark_score = score_one(current, horizons[horizon]["price_usd"], actual)
                benchmark_scores.setdefault(name, []).append(benchmark_score)
                regime_benchmarks.setdefault(name, []).append(benchmark_score)

        models = {name: _aggregate_scores(scores) for name, scores in sorted(per_model.items())}
        models["adaptive_ensemble"]["q10_q90_coverage"] = round(
            float(np.mean(ensemble_coverage)), 4
        )

        by_regime = {
            regime: {
                "samples": len(scores),
                "mae_pct": round(float(np.mean([s["absolute_error_pct"] for s in scores])), 4),
                "direction_accuracy": round(
                    float(np.mean([s["direction_correct"] for s in scores])), 4
                ),
            }
            for regime, scores in sorted(regime_scores.items())
        }

        benchmarks = {
            name: _aggregate_scores(scores) for name, scores in sorted(benchmark_scores.items())
        }
        benchmarks_by_regime = {
            regime: {
                name: _aggregate_scores(scores) for name, scores in sorted(per_benchmark.items())
            }
            for regime, per_benchmark in sorted(benchmark_regime_scores.items())
        }

        best_benchmark = min(benchmarks, key=lambda name: benchmarks[name]["mae_pct"])
        adaptive_mae = float(models["adaptive_ensemble"]["mae_pct"])
        persistence_mae = float(benchmarks["persistence"]["mae_pct"])
        best_mae = float(benchmarks[best_benchmark]["mae_pct"])
        comparison = {
            "best_benchmark": best_benchmark,
            "best_benchmark_mae_pct": best_mae,
            "adaptive_minus_persistence_mae_pct": round(adaptive_mae - persistence_mae, 4),
            "adaptive_minus_best_benchmark_mae_pct": round(adaptive_mae - best_mae, 4),
        }

        result[horizon] = {
            "models": models,
            "adaptive_ensemble_by_regime": by_regime,
            "benchmarks": benchmarks,
            "benchmarks_by_regime": benchmarks_by_regime,
            "benchmark_comparison": comparison,
        }
    return result


def history_snapshot(forecast: dict[str, Any]) -> dict[str, Any]:
    return {
        "latest_close_at": forecast["latest_close_at"],
        "latest_close_usd": forecast["latest_close_usd"],
        "regime": forecast["regime"],
        "model_predictions": forecast["model_predictions"],
        "predictions": forecast.get("predictions", {}),
        "model_weights": forecast.get("model_weights", {}),
    }


def evaluate_cross_validation(
    samples: list[dict[str, Any]],
    actual_by_timestamp: dict[int, float],
    folds: list[WalkForwardFold],
) -> dict[str, Any]:
    """Replay adaptive weighting inside purged chronological validation folds.

    Frozen per-model predictions are reused, but adaptive weights are recomputed
    from each fold's permitted history. At every validation origin, the actual
    lookup is truncated to timestamps already observable at that moment.
    """
    origin_timestamps = [int(sample["origin_timestamp"]) for sample in samples]
    aggregate_adaptive: dict[str, list[dict[str, Any]]] = {horizon: [] for horizon in HORIZONS}
    aggregate_benchmarks: dict[str, dict[str, list[dict[str, Any]]]] = {
        horizon: {name: [] for name in BENCHMARK_NAMES} for horizon in HORIZONS
    }
    fold_reports: list[dict[str, Any]] = []

    for fold in folds:
        assert_no_fold_leakage(
            fold,
            origin_timestamps,
            max_target_hours=max(TARGET_HOURS),
        )
        history = [history_snapshot(samples[index]["forecast"]) for index in fold.train_indices]
        history = history[-DEFAULT_HISTORY_LIMIT:]
        fold_adaptive: dict[str, list[dict[str, Any]]] = {horizon: [] for horizon in HORIZONS}
        fold_benchmarks: dict[str, dict[str, list[dict[str, Any]]]] = {
            horizon: {name: [] for name in BENCHMARK_NAMES} for horizon in HORIZONS
        }

        for index in fold.validation_indices:
            sample = samples[index]
            current = float(sample["current_price"])
            current_timestamp = int(sample["origin_timestamp"])
            regime = str(sample["forecast"]["regime"])
            model_predictions = sample["forecast"]["model_predictions"]
            model_names = list(model_predictions)
            visible_actuals = {
                timestamp: price
                for timestamp, price in actual_by_timestamp.items()
                if timestamp <= current_timestamp
            }

            for hour in TARGET_HOURS:
                horizon = f"{hour}h"
                weights, diagnostics = adaptive_model_weights(
                    model_names,
                    regime,
                    hour,
                    history,
                    visible_actuals,
                    history_limit=DEFAULT_HISTORY_LIMIT,
                )
                predicted = weighted_ensemble_price(
                    current,
                    model_predictions,
                    horizon,
                    weights,
                )
                score = score_one(current, predicted, float(sample["actuals"][horizon]))
                score["weighting_mode"] = diagnostics["mode"]
                fold_adaptive[horizon].append(score)
                aggregate_adaptive[horizon].append(score)

                for name, horizons in sample["benchmarks"].items():
                    benchmark_score = score_one(
                        current,
                        float(horizons[horizon]["price_usd"]),
                        float(sample["actuals"][horizon]),
                    )
                    fold_benchmarks[horizon][name].append(benchmark_score)
                    aggregate_benchmarks[horizon][name].append(benchmark_score)

            history.append(history_snapshot(sample["forecast"]))
            history = history[-DEFAULT_HISTORY_LIMIT:]

        by_horizon: dict[str, Any] = {}
        fold_maes: list[float] = []
        fold_directions: list[float] = []
        for horizon in HORIZONS:
            adaptive = _aggregate_scores(fold_adaptive[horizon])
            benchmarks = {
                name: _aggregate_scores(scores)
                for name, scores in sorted(fold_benchmarks[horizon].items())
            }
            best_benchmark = min(benchmarks, key=lambda name: benchmarks[name]["mae_pct"])
            by_horizon[horizon] = {
                "adaptive_ensemble": adaptive,
                "benchmarks": benchmarks,
                "best_benchmark": best_benchmark,
            }
            fold_maes.append(float(adaptive["mae_pct"]))
            fold_directions.append(float(adaptive["direction_accuracy"]))

        fold_reports.append(
            {
                "definition": fold_definition(fold, origin_timestamps),
                "objective_mae_pct": round(float(np.mean(fold_maes)), 6),
                "mean_direction_accuracy": round(float(np.mean(fold_directions)), 6),
                "by_horizon": by_horizon,
            }
        )

    aggregate_by_horizon: dict[str, Any] = {}
    dispersion_by_horizon: dict[str, Any] = {}
    for horizon in HORIZONS:
        benchmarks = {
            name: _aggregate_scores(scores)
            for name, scores in sorted(aggregate_benchmarks[horizon].items())
        }
        aggregate_by_horizon[horizon] = {
            "adaptive_ensemble": _aggregate_scores(aggregate_adaptive[horizon]),
            "benchmarks": benchmarks,
            "best_benchmark": min(benchmarks, key=lambda name: benchmarks[name]["mae_pct"]),
        }
        horizon_fold_maes = [
            float(fold["by_horizon"][horizon]["adaptive_ensemble"]["mae_pct"])
            for fold in fold_reports
        ]
        horizon_fold_directions = [
            float(fold["by_horizon"][horizon]["adaptive_ensemble"]["direction_accuracy"])
            for fold in fold_reports
        ]
        dispersion_by_horizon[horizon] = {
            "mae_pct": _dispersion(horizon_fold_maes),
            "direction_accuracy": _dispersion(horizon_fold_directions),
        }

    return {
        "folds": fold_reports,
        "aggregate_by_horizon": aggregate_by_horizon,
        "dispersion_by_horizon": dispersion_by_horizon,
        "objective_mae_pct_across_folds": _dispersion(
            [float(fold["objective_mae_pct"]) for fold in fold_reports]
        ),
        "direction_accuracy_across_folds": _dispersion(
            [float(fold["mean_direction_accuracy"]) for fold in fold_reports]
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--samples", type=int, default=60)
    parser.add_argument("--cv-folds", type=int, default=DEFAULT_CV_FOLDS)
    parser.add_argument("--cv-mode", choices=("expanding", "rolling"), default="expanding")
    parser.add_argument("--cv-min-train-samples", type=int, default=DEFAULT_MIN_TRAIN_SAMPLES)
    parser.add_argument("--cv-purge-hours", type=int, default=DEFAULT_PURGE_HOURS)
    parser.add_argument("--cv-embargo-hours", type=int, default=DEFAULT_EMBARGO_HOURS)
    parser.add_argument(
        "--cv-rolling-train-samples",
        type=int,
        default=DEFAULT_ROLLING_TRAIN_SAMPLES,
    )
    args = parser.parse_args()

    seed_everything()
    data = fetch_binance_history(args.days)
    first = 513
    last = len(data.closes) - max(TARGET_HOURS) - 1
    if last <= first:
        raise RuntimeError("Historical window is too small for the requested backtest")

    candidate_indices = np.linspace(first, last, num=min(args.samples, last - first + 1), dtype=int)
    indices = sorted(set(map(int, candidate_indices)))
    model = load_timesfm()
    samples: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []

    for number, index in enumerate(indices, start=1):
        context = slice_market(data, index)
        forecast = build_forecast(model, context, history=history)
        benchmarks = benchmark_forecasts(context)
        actuals = {f"{hour}h": float(data.closes[index + hour]) for hour in TARGET_HOURS}
        origin_timestamp = int(data.timestamps[index])
        samples.append(
            {
                "origin_at": datetime.fromtimestamp(
                    origin_timestamp, tz=timezone.utc
                ).isoformat(),
                "origin_timestamp": origin_timestamp,
                "current_price": float(data.closes[index]),
                "actuals": actuals,
                "forecast": forecast,
                "benchmarks": benchmarks,
            }
        )
        history.append(history_snapshot(forecast))
        history = history[-DEFAULT_HISTORY_LIMIT:]
        modes = sorted({str(p["weighting_mode"]) for p in forecast["predictions"].values()})
        print(
            f"Backtest {number}/{len(indices)}: {samples[-1]['origin_at']} "
            f"({forecast['regime']}; {','.join(modes)})"
        )

    origin_timestamps = [int(sample["origin_timestamp"]) for sample in samples]
    cv_folds = build_purged_walk_forward_folds(
        origin_timestamps,
        folds=args.cv_folds,
        min_train_samples=args.cv_min_train_samples,
        purge_hours=args.cv_purge_hours,
        embargo_hours=args.cv_embargo_hours,
        mode=args.cv_mode,
        rolling_train_samples=args.cv_rolling_train_samples,
        max_target_hours=max(TARGET_HOURS),
    )
    cv_definitions = [fold_definition(fold, origin_timestamps) for fold in cv_folds]
    actual_by_timestamp = dict(zip(data.timestamps, map(float, data.closes), strict=True))
    cross_validation = evaluate_cross_validation(samples, actual_by_timestamp, cv_folds)

    generated_at = datetime.now(timezone.utc)
    data_source = "Binance BTCUSDT 1h (historical proxy for BTC/USD)"
    cv_parameters = {
        "mode": args.cv_mode,
        "folds_requested": args.cv_folds,
        "min_train_samples": args.cv_min_train_samples,
        "purge_hours": args.cv_purge_hours,
        "embargo_hours": args.cv_embargo_hours,
        "rolling_train_samples": args.cv_rolling_train_samples,
        "max_target_hours": max(TARGET_HOURS),
        "fold_definitions": cv_definitions,
    }
    experiment_manifest = build_experiment_manifest(
        run_type="backtest",
        data=data,
        data_source=data_source,
        data_pair="BTC/USDT",
        run_parameters={
            "days_requested": args.days,
            "samples_requested": args.samples,
            "adaptive_history_limit": DEFAULT_HISTORY_LIMIT,
            "benchmark_models": list(BENCHMARK_NAMES),
            "cross_validation": cv_parameters,
        },
        model_names=sorted(samples[-1]["forecast"]["model_predictions"]) if samples else [],
        created_at=generated_at,
    )
    report = {
        "generated_at": generated_at.isoformat(),
        "data_source": data_source,
        "experiment_manifest": experiment_manifest,
        "benchmark_suite": benchmark_metadata(),
        "adaptive_history_limit": DEFAULT_HISTORY_LIMIT,
        "days_requested": args.days,
        "samples": len(samples),
        "summary": summarize(samples),
        "cross_validation": {
            "configuration": cv_parameters,
            **cross_validation,
        },
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n")

    print("\nBacktest summary")
    for horizon, info in report["summary"].items():
        adaptive = info["models"]["adaptive_ensemble"]
        persistence = info["benchmarks"]["persistence"]
        comparison = info["benchmark_comparison"]
        best_name = comparison["best_benchmark"]
        best = info["benchmarks"][best_name]
        print(
            f"{horizon}: adaptive MAE {adaptive['mae_pct']:.3f}% / "
            f"persistence {persistence['mae_pct']:.3f}% / "
            f"best benchmark {best_name} {best['mae_pct']:.3f}%"
        )

    print("\nPurged walk-forward cross-validation")
    for horizon, info in report["cross_validation"]["aggregate_by_horizon"].items():
        adaptive = info["adaptive_ensemble"]
        dispersion = report["cross_validation"]["dispersion_by_horizon"][horizon]["mae_pct"]
        print(
            f"{horizon}: adaptive MAE {adaptive['mae_pct']:.3f}% / "
            f"fold mean {dispersion['mean']:.3f}% / fold std {dispersion['std']:.3f}%"
        )
    print(f"\nSaved {REPORT_PATH}")


if __name__ == "__main__":
    main()
