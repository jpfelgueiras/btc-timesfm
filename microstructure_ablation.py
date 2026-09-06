#!/usr/bin/env python3
"""Leakage-safe walk-forward ablation for persisted microstructure features."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from microstructure_signals import MICROSTRUCTURE_FEATURE_NAMES
from statistical_significance import paired_bootstrap_comparison

REPORT_PATH = Path("microstructure_ablation_report.json")
SUMMARY_PATH = Path("microstructure_ablation_summary.md")
DEFAULT_DB_PATH = Path(".state/forecast_history.sqlite")
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


def _parse_time(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.astimezone(timezone.utc).timestamp())


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


def load_rows(path: Path) -> list[dict[str, Any]]:
    """Load only matured ensemble outcomes and immutable origin-time features."""
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT o.origin_at, o.regime, o.market_features_json,
                   p.horizon_hours, p.target_at, p.actual_change_pct
            FROM forecast_origins AS o
            JOIN forecast_predictions AS p USING(origin_at)
            WHERE p.model_name = 'ensemble'
              AND p.actual_target_price_usd IS NOT NULL
              AND p.horizon_hours IN (2, 4, 8, 16)
            ORDER BY o.origin_at, p.horizon_hours
            """
        ).fetchall()
    finally:
        connection.close()

    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            features = json.loads(row["market_features_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(features, dict):
            continue
        base = _vector(features, BASE_FEATURE_NAMES)
        micro = _vector(features, MICROSTRUCTURE_FEATURE_NAMES)
        if base is None or micro is None or row["actual_change_pct"] is None:
            continue
        result.append(
            {
                "origin_at": str(row["origin_at"]),
                "origin_s": _parse_time(str(row["origin_at"])),
                "target_s": _parse_time(str(row["target_at"])),
                "regime": str(row["regime"] or "unknown"),
                "horizon": int(row["horizon_hours"]),
                "base": base,
                "micro": micro,
                "target": float(row["actual_change_pct"]),
            }
        )
    return result


def eligible_training_rows(
    rows: list[dict[str, Any]], current_origin_s: int, horizon: int
) -> list[dict[str, Any]]:
    """Only labels already observable by the simulated forecast origin may train."""
    return [
        row
        for row in rows
        if int(row["horizon"]) == horizon and int(row["target_s"]) <= current_origin_s
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


def evaluate_rows(rows: list[dict[str, Any]], *, min_train: int = 24) -> dict[str, Any]:
    """Compare identical walk-forward origins with and without microstructure."""
    report: dict[str, Any] = {"min_train": min_train, "horizons": {}}
    for horizon in HORIZONS:
        horizon_rows = sorted(
            [row for row in rows if int(row["horizon"]) == horizon],
            key=lambda row: int(row["origin_s"]),
        )
        outcomes: list[dict[str, Any]] = []
        for test in horizon_rows:
            train = eligible_training_rows(horizon_rows, int(test["origin_s"]), horizon)
            if len(train) < min_train:
                continue
            base_x = np.asarray([row["base"] for row in train], dtype=float)
            augmented_x = np.asarray([row["base"] + row["micro"] for row in train], dtype=float)
            train_y = np.asarray([row["target"] for row in train], dtype=float)
            baseline = _ridge_predict(base_x, train_y, np.asarray(test["base"], dtype=float))
            candidate = _ridge_predict(
                augmented_x,
                train_y,
                np.asarray(test["base"] + test["micro"], dtype=float),
            )
            actual = float(test["target"])
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

        candidate_errors = [item["candidate_error"] for item in outcomes]
        baseline_errors = [item["baseline_error"] for item in outcomes]
        significance = paired_bootstrap_comparison(
            candidate_errors,
            baseline_errors,
            metric="absolute_change_error_pct_points",
            lower_is_better=True,
            min_samples=32,
            iterations=2000,
            seed=horizon,
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
        report["horizons"][f"{horizon}h"] = {
            "available_feature_rows": len(horizon_rows),
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
        "# Microstructure feature ablation",
        "",
        "Real order-book features are evaluated only after they have been collected live and persisted with immutable forecast origins.",
        "",
        "| Horizon | Rows | WF samples | Baseline MAE | +Microstructure MAE | Evidence |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for horizon, item in report["horizons"].items():
        baseline = item["baseline_mae_pp"]
        candidate = item["candidate_mae_pp"]
        lines.append(
            f"| {horizon} | {item['available_feature_rows']} | {item['walk_forward_samples']} | "
            f"{baseline if baseline is not None else '--'} | "
            f"{candidate if candidate is not None else '--'} | {item['recommendation']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--min-train", type=int, default=24)
    args = parser.parse_args()
    rows = load_rows(args.db)
    report = evaluate_rows(rows, min_train=args.min_train)
    report["database"] = str(args.db)
    report["feature_names"] = list(MICROSTRUCTURE_FEATURE_NAMES)
    REPORT_PATH.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    SUMMARY_PATH.write_text(render_summary(report))
    print(render_summary(report))


if __name__ == "__main__":
    main()
