#!/usr/bin/env python3
"""Walk-forward ablation for BTC derivatives features.

The comparison is deliberately lightweight and research-only: a ridge model with
spot-market features is compared with the exact same model plus funding, open
interest and liquidation features. Each prediction trains only on rows whose
forecast target has already matured by the simulated origin.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from backtest import fetch_binance_history, slice_market
from derivatives_signals import (
    DERIVATIVE_FEATURE_NAMES,
    fetch_derivatives_history,
    snapshot_from_rows,
)
from experiment_manifest import seed_everything
from forecast_engine import TARGET_HOURS, market_features
from statistical_significance import paired_bootstrap_comparison


REPORT_PATH = Path("derivatives_ablation_report.json")
SUMMARY_PATH = Path("derivatives_ablation_summary.md")
DEFAULT_DAYS = 30
DEFAULT_SAMPLES = 96
DEFAULT_MIN_TRAIN = 48
HORIZONS = tuple(f"{hour}h" for hour in TARGET_HOURS)
MARKET_FEATURE_NAMES = (
    "volatility_24h_pct",
    "range_24h_avg_pct",
    "volume_zscore_7d",
    "rsi_14",
    "momentum_6h_pct",
    "momentum_24h_pct",
    "momentum_7d_pct",
)


def _vector(features: dict[str, Any], names: tuple[str, ...]) -> list[float] | None:
    values: list[float] = []
    for name in names:
        value = features.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        values.append(float(value))
    return values


def _ridge_predict(
    train_x: np.ndarray,
    train_y: np.ndarray,
    test_x: np.ndarray,
    ridge: float = 1.0,
) -> float:
    means = np.mean(train_x, axis=0)
    scales = np.std(train_x, axis=0, ddof=0)
    scales = np.where(scales < 1e-9, 1.0, scales)
    standardized = (train_x - means) / scales
    test_standardized = (test_x - means) / scales
    design = np.column_stack([np.ones(len(standardized)), standardized])
    penalty = np.eye(design.shape[1], dtype=np.float64) * ridge
    penalty[0, 0] = 0.0
    coefficients = np.linalg.solve(design.T @ design + penalty, design.T @ train_y)
    return float(np.dot(np.concatenate([[1.0], test_standardized]), coefficients))


def eligible_training_rows(
    rows: list[dict[str, Any]], current_origin_s: int, horizon_hours: int
) -> list[dict[str, Any]]:
    """Return only rows whose target was observable before current_origin_s."""
    return [
        row
        for row in rows
        if int(row["origin_timestamp"]) + horizon_hours * 3600 <= current_origin_s
    ]


def build_feature_rows(
    data: Any,
    derivatives_history: dict[str, list[dict[str, Any]]],
    *,
    samples: int,
) -> list[dict[str, Any]]:
    first = 513
    last = len(data.closes) - max(TARGET_HOURS) - 1
    if last <= first:
        raise RuntimeError("Historical window is too small for derivatives ablation")
    count = min(max(1, samples), last - first + 1)
    indices = sorted(set(map(int, np.linspace(first, last, num=count, dtype=int))))
    rows: list[dict[str, Any]] = []

    for index in indices:
        origin_s = int(data.timestamps[index])
        origin = datetime.fromtimestamp(origin_s, tz=timezone.utc)
        context = slice_market(data, index)
        spot_features = market_features(context)
        derivatives = snapshot_from_rows(
            origin,
            derivatives_history.get("funding", []),
            derivatives_history.get("stats", []),
        )
        market_vector = _vector(spot_features, MARKET_FEATURE_NAMES)
        derivatives_vector = _vector(derivatives.get("features", {}), DERIVATIVE_FEATURE_NAMES)
        if market_vector is None or derivatives_vector is None:
            continue
        current_price = float(data.closes[index])
        targets = {
            f"{hour}h": math.log(float(data.closes[index + hour]) / current_price)
            for hour in TARGET_HOURS
        }
        rows.append(
            {
                "origin_at": origin.isoformat(),
                "origin_timestamp": origin_s,
                "current_price": current_price,
                "market_features": market_vector,
                "derivatives_features": derivatives_vector,
                "targets": targets,
            }
        )
    return rows


def _score(current: float, predicted_return: float, actual_return: float) -> dict[str, Any]:
    predicted = current * math.exp(predicted_return)
    actual = current * math.exp(actual_return)
    predicted_direction = 1 if predicted_return > 0 else -1 if predicted_return < 0 else 0
    actual_direction = 1 if actual_return > 0 else -1 if actual_return < 0 else 0
    return {
        "absolute_error_pct": abs(predicted - actual) / actual * 100.0,
        "signed_error_pct": (predicted - actual) / actual * 100.0,
        "direction_correct": predicted_direction == actual_direction,
    }


def _aggregate(scores: list[dict[str, Any]]) -> dict[str, Any]:
    if not scores:
        return {"samples": 0, "mae_pct": None, "bias_pct": None, "direction_accuracy": None}
    return {
        "samples": len(scores),
        "mae_pct": round(float(np.mean([item["absolute_error_pct"] for item in scores])), 6),
        "bias_pct": round(float(np.mean([item["signed_error_pct"] for item in scores])), 6),
        "direction_accuracy": round(
            float(np.mean([item["direction_correct"] for item in scores])), 6
        ),
    }


def walk_forward_ablation(
    rows: list[dict[str, Any]], *, min_train: int = DEFAULT_MIN_TRAIN
) -> dict[str, Any]:
    by_horizon: dict[str, Any] = {}
    for hour in TARGET_HOURS:
        horizon = f"{hour}h"
        baseline_scores: list[dict[str, Any]] = []
        augmented_scores: list[dict[str, Any]] = []
        baseline_errors: list[float] = []
        augmented_errors: list[float] = []
        origins: list[str] = []

        for row in rows:
            origin_s = int(row["origin_timestamp"])
            training = eligible_training_rows(rows, origin_s, hour)
            if len(training) < min_train:
                continue
            train_y = np.asarray([item["targets"][horizon] for item in training], dtype=np.float64)
            market_x = np.asarray([item["market_features"] for item in training], dtype=np.float64)
            augmented_x = np.asarray(
                [item["market_features"] + item["derivatives_features"] for item in training],
                dtype=np.float64,
            )
            baseline_return = _ridge_predict(
                market_x,
                train_y,
                np.asarray(row["market_features"], dtype=np.float64),
            )
            augmented_return = _ridge_predict(
                augmented_x,
                train_y,
                np.asarray(row["market_features"] + row["derivatives_features"], dtype=np.float64),
            )
            actual_return = float(row["targets"][horizon])
            baseline_score = _score(float(row["current_price"]), baseline_return, actual_return)
            augmented_score = _score(float(row["current_price"]), augmented_return, actual_return)
            baseline_scores.append(baseline_score)
            augmented_scores.append(augmented_score)
            baseline_errors.append(float(baseline_score["absolute_error_pct"]))
            augmented_errors.append(float(augmented_score["absolute_error_pct"]))
            origins.append(str(row["origin_at"]))

        baseline = _aggregate(baseline_scores)
        augmented = _aggregate(augmented_scores)
        base_mae = baseline["mae_pct"]
        new_mae = augmented["mae_pct"]
        improvement = (
            (float(base_mae) - float(new_mae)) / float(base_mae)
            if base_mae not in (None, 0) and new_mae is not None
            else None
        )
        significance = paired_bootstrap_comparison(
            augmented_errors,
            baseline_errors,
            metric=f"{horizon}_mae_pct",
            lower_is_better=True,
        )
        by_horizon[horizon] = {
            "origins": origins,
            "market_only": baseline,
            "market_plus_derivatives": augmented,
            "relative_mae_improvement": round(improvement, 6) if improvement is not None else None,
            "direction_accuracy_delta": (
                round(
                    float(augmented["direction_accuracy"]) - float(baseline["direction_accuracy"]),
                    6,
                )
                if augmented["direction_accuracy"] is not None
                and baseline["direction_accuracy"] is not None
                else None
            ),
            "significance": significance,
        }

    improvements = [
        float(item["relative_mae_improvement"])
        for item in by_horizon.values()
        if item["relative_mae_improvement"] is not None
    ]
    safe = bool(improvements) and min(improvements) >= -0.05
    significant = sum(
        item["significance"].get("conclusion") == "candidate_better" for item in by_horizon.values()
    )
    mean_improvement = float(np.mean(improvements)) if improvements else 0.0
    recommendation = (
        "edge_detected"
        if mean_improvement >= 0.01 and safe and significant >= 1
        else "no_defensible_edge"
    )
    return {
        "schema_version": 1,
        "method": "paired_leakage_safe_walk_forward_ridge_ablation",
        "uses_future_information": False,
        "minimum_training_rows": min_train,
        "feature_sets": {
            "market_only": list(MARKET_FEATURE_NAMES),
            "market_plus_derivatives": list(MARKET_FEATURE_NAMES + DERIVATIVE_FEATURE_NAMES),
        },
        "by_horizon": by_horizon,
        "overall": {
            "mean_relative_mae_improvement": round(mean_improvement, 6),
            "no_material_horizon_regression": safe,
            "statistically_better_horizons": significant,
            "recommendation": recommendation,
        },
    }


def render_summary(report: dict[str, Any]) -> str:
    lines = [
        "# BTC derivatives feature ablation",
        "",
        f"- Recommendation: **{report['overall']['recommendation']}**",
        f"- Mean relative MAE improvement: **{report['overall']['mean_relative_mae_improvement'] * 100:.2f}%**",
        f"- Leakage-safe: **{'yes' if not report['uses_future_information'] else 'no'}**",
        "",
        "| Horizon | Samples | Market-only MAE | +Derivatives MAE | Relative improvement | Direction delta | Evidence |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for horizon in HORIZONS:
        item = report["by_horizon"][horizon]
        baseline = item["market_only"]
        augmented = item["market_plus_derivatives"]
        improvement = item["relative_mae_improvement"]
        direction = item["direction_accuracy_delta"]
        lines.append(
            f"| {horizon} | {augmented['samples']} | "
            f"{baseline['mae_pct'] if baseline['mae_pct'] is not None else 'n/a'} | "
            f"{augmented['mae_pct'] if augmented['mae_pct'] is not None else 'n/a'} | "
            f"{improvement * 100:.2f}% | {direction if direction is not None else 'n/a'} | "
            f"{item['significance'].get('conclusion', 'inconclusive')} |"
            if improvement is not None
            else f"| {horizon} | 0 | n/a | n/a | n/a | n/a | inconclusive |"
        )
    lines.extend(
        [
            "",
            "This is a research ablation only. Derivatives features are not allowed to alter production forecasts solely because this report exists.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run derivatives feature walk-forward ablation")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    parser.add_argument("--min-train", type=int, default=DEFAULT_MIN_TRAIN)
    args = parser.parse_args()
    days = min(30, max(24, args.days))
    seed_everything()
    data = fetch_binance_history(days)
    start = datetime.fromtimestamp(data.timestamps[0], tz=timezone.utc) - timedelta(hours=24)
    end = datetime.fromtimestamp(data.timestamps[-1], tz=timezone.utc)
    history = fetch_derivatives_history(start, end)
    rows = build_feature_rows(data, history, samples=args.samples)
    report = walk_forward_ablation(rows, min_train=max(8, args.min_train))
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    report["research_window_days"] = days
    report["eligible_feature_rows"] = len(rows)
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    SUMMARY_PATH.write_text(render_summary(report), encoding="utf-8")
    print(SUMMARY_PATH.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
