#!/usr/bin/env python3
"""Leakage-safe walk-forward ablation for lower-timeframe market context.

The comparison is deliberately lightweight and research-only: a ridge model with
hourly spot features is compared with the exact same model plus compact 5m/15m
aggregates derived from aligned high-frequency candles. Each prediction trains
only on rows whose forecast target has already matured by the simulated origin,
and every feature only uses candles whose timestamp is at or before that origin.

The entrypoint accepts synthetic high-frequency candle arrays directly (for
tests) and a JSON file of candles for the CLI. Evaluation makes no network
requests, so runtime and API cost stay bounded for GitHub Actions.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from btc_timesfm.data.multi_resolution import (
    MULTI_RESOLUTION_FEATURE_NAMES,
    SCHEMA_VERSION,
    feature_set_version,
    recipe_sha256,
    summarize_if_available,
)
from btc_timesfm.forecasting.forecast_engine import MarketData, detect_regime, market_features
from btc_timesfm.forecasting.statistical_significance import paired_bootstrap_comparison

REPORT_PATH = Path("multiresolution_ablation_report.json")
SUMMARY_PATH = Path("multiresolution_ablation_summary.md")
DEFAULT_CANDLES_PATH = Path("multiresolution_candles.json")
DEFAULT_SAMPLES = 96
DEFAULT_MIN_TRAIN = 32
HORIZONS = (2, 4, 8, 16)
BASE_FEATURE_NAMES = (
    "volatility_24h_pct",
    "range_24h_avg_pct",
    "volume_zscore_7d",
    "rsi_14",
    "momentum_6h_pct",
    "momentum_24h_pct",
    "momentum_7d_pct",
)


def _slice_market(data: MarketData, end_index: int, context: int = 513) -> MarketData:
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


def _vector(features: dict[str, Any], names: tuple[str, ...]) -> list[float] | None:
    values: list[float] = []
    for name in names:
        value = features.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        if not math.isfinite(number):
            return None
        values.append(number)
    return values


def _to_market_data(payload: dict[str, Any]) -> MarketData:
    timestamps = [int(value) for value in payload["timestamps"]]
    closes = np.asarray(payload["closes"], dtype=np.float32)
    if len(timestamps) != len(closes):
        raise ValueError("hourly timestamps and closes must have the same length")
    return MarketData(
        timestamps=timestamps,
        opens=np.asarray(payload["opens"], dtype=np.float32),
        highs=np.asarray(payload["highs"], dtype=np.float32),
        lows=np.asarray(payload["lows"], dtype=np.float32),
        closes=closes,
        volumes=np.asarray(payload["volumes"], dtype=np.float32),
    )


def high_frequency_from_json(path: Path) -> dict[str, list[Any]]:
    """Load a JSON object of high-frequency candle columns."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    section = payload.get("high_frequency") if isinstance(payload, dict) else None
    if not isinstance(section, dict):
        raise ValueError(f"{path} must be a JSON object with a high_frequency section")
    return {
        name: section.get(name, [])
        for name in ("timestamps", "opens", "highs", "lows", "closes", "volumes")
    }


def load_candles(path: Path) -> tuple[MarketData, dict[str, list[Any]]]:
    """Load hourly + high-frequency candles from one JSON document."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    hourly_payload = payload.get("hourly")
    if not isinstance(hourly_payload, dict):
        raise ValueError(f"{path} must contain an hourly OHLCV section")
    data = _to_market_data(hourly_payload)
    high_freq = high_frequency_from_json(path)
    return data, high_freq


def build_feature_rows(
    data: MarketData,
    high_freq: Any,
    *,
    samples: int = DEFAULT_SAMPLES,
) -> list[dict[str, Any]]:
    """Build timestamp-bounded research rows from hourly + high-frequency candles."""
    first = 512
    last = len(data.closes) - max(HORIZONS) - 1
    if last <= first:
        raise RuntimeError("Hourly window is too small for the multiresolution ablation")
    count = min(max(1, samples), last - first + 1)
    indices = sorted(set(map(int, np.linspace(first, last, num=count, dtype=int))))
    rows: list[dict[str, Any]] = []

    for index in indices:
        context = _slice_market(data, index)
        origin_s = int(context.timestamps[-1])
        origin = datetime.fromtimestamp(origin_s, tz=timezone.utc)
        base_features = market_features(context)
        base_vector = _vector(base_features, BASE_FEATURE_NAMES)
        if base_vector is None:
            continue
        summary = summarize_if_available(high_freq, origin_s)
        summary_features = summary.get("features")
        multi_vector = (
            _vector(summary_features, MULTI_RESOLUTION_FEATURE_NAMES)
            if isinstance(summary_features, dict)
            else None
        )
        current = float(data.closes[index])
        rows.append(
            {
                "origin_at": origin.isoformat(),
                "origin_s": origin_s,
                "regime": detect_regime(base_features),
                "base": base_vector,
                "multi": multi_vector,
                "multi_available": multi_vector is not None,
                "multi_version": summary.get("feature_set_version"),
                "targets": {
                    f"{horizon}h": (float(data.closes[index + horizon]) / current - 1.0) * 100.0
                    for horizon in HORIZONS
                },
            }
        )
    return rows


def eligible_training_rows(
    rows: list[dict[str, Any]], current_origin_s: int, horizon: int
) -> list[dict[str, Any]]:
    """A training target is usable only after its exact horizon matured."""
    return [
        row
        for row in rows
        if row.get("multi") is not None
        and int(row["origin_s"]) + horizon * 3600 <= current_origin_s
    ]


def _ridge_predict(train_x: np.ndarray, train_y: np.ndarray, test_x: np.ndarray) -> float:
    means = np.mean(train_x, axis=0)
    scales = np.std(train_x, axis=0, ddof=0)
    scales = np.where(scales < 1e-9, 1.0, scales)
    standardized = (train_x - means) / scales
    test_standardized = (test_x - means) / scales
    design = np.column_stack([np.ones(len(standardized)), standardized])
    penalty: np.ndarray = np.eye(design.shape[1], dtype=float)
    penalty[0, 0] = 0.0
    coefficients = np.linalg.solve(design.T @ design + penalty, design.T @ train_y)
    return float(np.dot(np.concatenate([[1.0], test_standardized]), coefficients))


def evaluate_multiresolution_ablation(
    rows: list[dict[str, Any]],
    *,
    min_train: int = DEFAULT_MIN_TRAIN,
    iterations: int = 2000,
) -> dict[str, Any]:
    """Compare identical walk-forward origins with and without lower-timeframe context."""
    started = time.perf_counter()
    ordered = sorted(rows, key=lambda row: int(row["origin_s"]))
    with_multi = [row for row in ordered if row.get("multi") is not None]

    by_horizon: dict[str, Any] = {}
    for horizon in HORIZONS:
        target_key = f"{horizon}h"
        outcomes: list[dict[str, Any]] = []
        for test in with_multi:
            train = eligible_training_rows(with_multi, int(test["origin_s"]), horizon)
            if len(train) < min_train:
                continue
            base_x = np.asarray([row["base"] for row in train], dtype=float)
            augmented_x = np.asarray([row["base"] + row["multi"] for row in train], dtype=float)
            train_y = np.asarray([row["targets"][target_key] for row in train], dtype=float)
            baseline = _ridge_predict(base_x, train_y, np.asarray(test["base"], dtype=float))
            candidate = _ridge_predict(
                augmented_x,
                train_y,
                np.asarray(test["base"] + test["multi"], dtype=float),
            )
            actual = float(test["targets"][target_key])
            outcomes.append(
                {
                    "origin_at": test["origin_at"],
                    "regime": test["regime"],
                    "baseline_error": abs(baseline - actual),
                    "candidate_error": abs(candidate - actual),
                    "baseline_direction": (baseline > 0) == (actual > 0),
                    "candidate_direction": (candidate > 0) == (actual > 0),
                }
            )

        baseline_errors = [item["baseline_error"] for item in outcomes]
        candidate_errors = [item["candidate_error"] for item in outcomes]
        significance = paired_bootstrap_comparison(
            candidate_errors,
            baseline_errors,
            metric="absolute_change_error_pct_points",
            lower_is_better=True,
            min_samples=32,
            iterations=iterations,
            seed=100 + horizon,
        )
        baseline_mae = round(float(np.mean(baseline_errors)), 6) if outcomes else None
        candidate_mae = round(float(np.mean(candidate_errors)), 6) if outcomes else None
        improvement = (
            (float(baseline_mae) - float(candidate_mae)) / float(baseline_mae)
            if baseline_mae not in (None, 0) and candidate_mae is not None
            else None
        )
        baseline_direction = (
            float(np.mean([item["baseline_direction"] for item in outcomes])) if outcomes else None
        )
        candidate_direction = (
            float(np.mean([item["candidate_direction"] for item in outcomes])) if outcomes else None
        )
        by_horizon[target_key] = {
            "walk_forward_samples": len(outcomes),
            "baseline_mae_pp": baseline_mae,
            "candidate_mae_pp": candidate_mae,
            "relative_mae_improvement": round(improvement, 6) if improvement is not None else None,
            "baseline_direction_accuracy": (
                round(baseline_direction, 6) if baseline_direction is not None else None
            ),
            "candidate_direction_accuracy": (
                round(candidate_direction, 6) if candidate_direction is not None else None
            ),
            "direction_accuracy_delta": (
                round(float(candidate_direction) - float(baseline_direction), 6)
                if candidate_direction is not None and baseline_direction is not None
                else None
            ),
            "significance": significance,
            "origins": [item["origin_at"] for item in outcomes],
            "recommendation": (
                "keep_for_research"
                if significance["conclusion"] == "candidate_better"
                else "drop_or_research"
                if significance["conclusion"] == "baseline_better"
                else "insufficient_evidence"
            ),
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
    elapsed = time.perf_counter() - started

    return {
        "schema_version": SCHEMA_VERSION,
        "method": "paired_leakage_safe_walk_forward_ridge_ablation",
        "uses_future_information": False,
        "minimum_training_rows": min_train,
        "feature_names": list(MULTI_RESOLUTION_FEATURE_NAMES),
        "feature_sets": {
            "market_only": list(BASE_FEATURE_NAMES),
            "market_plus_multiresolution": list(
                BASE_FEATURE_NAMES + MULTI_RESOLUTION_FEATURE_NAMES
            ),
        },
        "feature_set_version": feature_set_version(),
        "feature_versioning": {
            "method": "sha256",
            "schema_version": SCHEMA_VERSION,
            "recipe_sha256": recipe_sha256(),
        },
        "availability": {
            "hourly_origins": len(ordered),
            "rows_with_multiresolution": len(with_multi),
            "rows_without_multiresolution": len(ordered) - len(with_multi),
            "degraded_to_hourly_only": len(with_multi) == 0,
        },
        "by_horizon": by_horizon,
        "overall": {
            "mean_relative_mae_improvement": round(mean_improvement, 6),
            "no_material_horizon_regression": safe,
            "statistically_better_horizons": significant,
            "recommendation": recommendation,
        },
        "runtime_estimate_seconds": round(elapsed, 4),
        "api_cost": {
            "network_requests": 0,
            "bounded_for_ci": True,
            "note": (
                "Evaluation is a pure function of provided candles: no network "
                "requests, a bounded ridge feature matrix, and capped bootstrap "
                "iterations keep Runtime/API cost bounded for GitHub Actions."
            ),
        },
    }


def render_summary(report: dict[str, Any]) -> str:
    lines = [
        "# Lower-timeframe market-context ablation",
        "",
        f"- Feature-set version: **{report['feature_set_version']}**",
        f"- Recommendation: **{report['overall']['recommendation']}**",
        f"- Mean relative MAE improvement: **{report['overall']['mean_relative_mae_improvement'] * 100:.2f}%**",
        f"- Leakage-safe: **{'yes' if not report['uses_future_information'] else 'no'}**",
        f"- Rows with multiresolution context: **{report['availability']['rows_with_multiresolution']}"
        f" / {report['availability']['hourly_origins']}**",
        f"- Estimated runtime: **{report['runtime_estimate_seconds']:.2f}s**",
        "",
        "| Horizon | WF samples | Baseline MAE | +Multi-res MAE | Relative improvement | Direction delta | Evidence |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for horizon in ("2h", "4h", "8h", "16h"):
        item = report["by_horizon"][horizon]
        improvement = item["relative_mae_improvement"]
        direction = item["direction_accuracy_delta"]
        lines.append(
            f"| {horizon} | {item['walk_forward_samples']} | "
            f"{item['baseline_mae_pp'] if item['baseline_mae_pp'] is not None else 'n/a'} | "
            f"{item['candidate_mae_pp'] if item['candidate_mae_pp'] is not None else 'n/a'} | "
            f"{improvement * 100:.2f}% | {direction if direction is not None else 'n/a'} | "
            f"{item['significance'].get('conclusion', 'inconclusive')} |"
            if improvement is not None
            else f"| {horizon} | 0 | n/a | n/a | n/a | n/a | inconclusive |"
        )
    lines.extend(
        [
            "",
            "This is a research ablation only. Lower-timeframe aggregates do not alter "
            "production forecasts until they demonstrate defensible out-of-sample value.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the multiresolution walk-forward ablation")
    parser.add_argument("--candles", type=Path, default=DEFAULT_CANDLES_PATH)
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    parser.add_argument("--min-train", type=int, default=DEFAULT_MIN_TRAIN)
    parser.add_argument("--out", type=Path, default=REPORT_PATH)
    args = parser.parse_args()

    data, high_freq = load_candles(args.candles)
    rows = build_feature_rows(data, high_freq, samples=args.samples)
    report = evaluate_multiresolution_ablation(rows, min_train=max(8, args.min_train))
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    report["feature_rows"] = len(rows)
    report_path = args.out
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    SUMMARY_PATH.write_text(render_summary(report), encoding="utf-8")
    print(render_summary(report))


if __name__ == "__main__":
    main()
