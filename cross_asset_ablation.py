#!/usr/bin/env python3
"""Walk-forward ablation for timestamp-safe cross-asset and macro features."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from cross_asset_signals import (
    CROSS_ASSET_FEATURE_NAMES,
    ETH_PAIR,
    US10Y_SERIES,
    VIX_SERIES,
    fetch_fred_series,
    fetch_kraken_pair_hourly,
    snapshot_from_inputs,
)
from forecast_engine import MarketData, detect_regime, market_features
from statistical_significance import paired_bootstrap_comparison

BTC_PAIR = "XBTUSD"
REPORT_PATH = Path("cross_asset_ablation_report.json")
SUMMARY_PATH = Path("cross_asset_ablation_summary.md")
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
DEFAULT_SAMPLES = 96
DEFAULT_MIN_TRAIN = 32


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


def build_feature_rows(
    btc: MarketData,
    eth: MarketData,
    vix: list[dict[str, Any]],
    us10y: list[dict[str, Any]],
    *,
    samples: int = DEFAULT_SAMPLES,
) -> list[dict[str, Any]]:
    """Build timestamp-bounded research rows; future external values are ignored."""
    first = 512
    last = len(btc.closes) - max(HORIZONS) - 1
    if last <= first:
        raise RuntimeError("Kraken history is too short for cross-asset ablation")
    count = min(max(1, samples), last - first + 1)
    indices = sorted(set(map(int, np.linspace(first, last, num=count, dtype=int))))
    rows: list[dict[str, Any]] = []

    for index in indices:
        context = _slice_market(btc, index)
        origin_s = int(context.timestamps[-1])
        origin = datetime.fromtimestamp(origin_s, tz=timezone.utc)
        base_features = market_features(context)
        snapshot = snapshot_from_inputs(origin, context, eth, vix, us10y)
        cross_features = snapshot.get("features")
        if not isinstance(cross_features, dict):
            continue
        base_vector = _vector(base_features, BASE_FEATURE_NAMES)
        cross_vector = _vector(cross_features, CROSS_ASSET_FEATURE_NAMES)
        if base_vector is None or cross_vector is None:
            continue
        current = float(btc.closes[index])
        rows.append(
            {
                "origin_at": origin.isoformat(),
                "origin_s": origin_s,
                "regime": detect_regime(base_features),
                "base": base_vector,
                "cross": cross_vector,
                "targets": {
                    f"{horizon}h": (float(btc.closes[index + horizon]) / current - 1.0) * 100.0
                    for horizon in HORIZONS
                },
            }
        )
    return rows


def eligible_training_rows(
    rows: list[dict[str, Any]], current_origin_s: int, horizon: int
) -> list[dict[str, Any]]:
    """A training target is usable only after its exact forecast horizon matured."""
    return [row for row in rows if int(row["origin_s"]) + horizon * 3600 <= current_origin_s]


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


def evaluate_rows(
    rows: list[dict[str, Any]], *, min_train: int = DEFAULT_MIN_TRAIN
) -> dict[str, Any]:
    report: dict[str, Any] = {"min_train": min_train, "horizons": {}}
    ordered = sorted(rows, key=lambda row: int(row["origin_s"]))
    for horizon in HORIZONS:
        outcomes: list[dict[str, Any]] = []
        target_key = f"{horizon}h"
        for test in ordered:
            train = eligible_training_rows(ordered, int(test["origin_s"]), horizon)
            if len(train) < min_train:
                continue
            base_x = np.asarray([row["base"] for row in train], dtype=float)
            cross_x = np.asarray([row["base"] + row["cross"] for row in train], dtype=float)
            train_y = np.asarray([row["targets"][target_key] for row in train], dtype=float)
            baseline = _ridge_predict(base_x, train_y, np.asarray(test["base"], dtype=float))
            candidate = _ridge_predict(
                cross_x,
                train_y,
                np.asarray(test["base"] + test["cross"], dtype=float),
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
            iterations=2000,
            seed=100 + horizon,
        )
        regimes: dict[str, Any] = {}
        for regime in sorted({str(item["regime"]) for item in outcomes}):
            subset = [item for item in outcomes if item["regime"] == regime]
            regimes[regime] = {
                "samples": len(subset),
                "baseline_mae_pp": round(
                    float(np.mean([item["baseline_error"] for item in subset])), 6
                ),
                "candidate_mae_pp": round(
                    float(np.mean([item["candidate_error"] for item in subset])), 6
                ),
                "baseline_direction_accuracy": round(
                    float(np.mean([item["baseline_direction"] for item in subset])), 6
                ),
                "candidate_direction_accuracy": round(
                    float(np.mean([item["candidate_direction"] for item in subset])), 6
                ),
            }
        report["horizons"][target_key] = {
            "walk_forward_samples": len(outcomes),
            "baseline_mae_pp": round(float(np.mean(baseline_errors)), 6) if outcomes else None,
            "candidate_mae_pp": round(float(np.mean(candidate_errors)), 6) if outcomes else None,
            "baseline_direction_accuracy": round(
                float(np.mean([item["baseline_direction"] for item in outcomes])), 6
            )
            if outcomes
            else None,
            "candidate_direction_accuracy": round(
                float(np.mean([item["candidate_direction"] for item in outcomes])), 6
            )
            if outcomes
            else None,
            "significance": significance,
            "regimes": regimes,
            "recommendation": (
                "keep_for_research"
                if significance["conclusion"] == "candidate_better"
                else "drop_or_research"
                if significance["conclusion"] == "baseline_better"
                else "insufficient_evidence"
            ),
        }
    return report


def render_summary(report: dict[str, Any]) -> str:
    lines = [
        "# Cross-asset and macro ablation",
        "",
        "| Horizon | WF samples | Baseline MAE | +Cross-asset MAE | Evidence |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for horizon, item in report["horizons"].items():
        lines.append(
            f"| {horizon} | {item['walk_forward_samples']} | "
            f"{item['baseline_mae_pp'] if item['baseline_mae_pp'] is not None else '--'} | "
            f"{item['candidate_mae_pp'] if item['candidate_mae_pp'] is not None else '--'} | "
            f"{item['recommendation']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    parser.add_argument("--min-train", type=int, default=DEFAULT_MIN_TRAIN)
    args = parser.parse_args()

    btc = fetch_kraken_pair_hourly(BTC_PAIR, 720)
    eth = fetch_kraken_pair_hourly(ETH_PAIR, 720)
    vix = fetch_fred_series(VIX_SERIES)
    us10y = fetch_fred_series(US10Y_SERIES)
    rows = build_feature_rows(btc, eth, vix, us10y, samples=args.samples)
    report = evaluate_rows(rows, min_train=args.min_train)
    report.update(
        {
            "feature_names": list(CROSS_ASSET_FEATURE_NAMES),
            "feature_rows": len(rows),
            "providers": {
                "btc": {"name": "kraken_spot", "pair": BTC_PAIR},
                "eth": {"name": "kraken_spot", "pair": ETH_PAIR},
                "vix": {"name": "fred", "series": VIX_SERIES},
                "us10y": {"name": "fred", "series": US10Y_SERIES},
            },
        }
    )
    REPORT_PATH.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    SUMMARY_PATH.write_text(render_summary(report))
    print(render_summary(report))


if __name__ == "__main__":
    main()
