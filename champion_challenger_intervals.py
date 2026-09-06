#!/usr/bin/env python3
"""Attach leakage-safe residual interval diagnostics to optimizer candidates.

The optimizer stores per-origin absolute errors for every horizon. For issue #42
we turn those already-paired errors into a causal interval-quality diagnostic:
each origin is evaluated against a symmetric residual band calibrated only from
older origins. Native interval metrics, when present, are never overwritten.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

HORIZONS = ("2h", "4h", "8h", "16h")
DEFAULT_MIN_HISTORY = 10
DEFAULT_WINDOW = 48


def _quantile_nearest_rank(values: list[float], quantile: float) -> float:
    if not values:
        raise ValueError("quantile requires at least one value")
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(quantile * len(ordered))))
    return ordered[rank - 1]


def causal_interval_diagnostic(
    absolute_errors_pct: list[float],
    *,
    target_coverage: float = 0.80,
    min_history: int = DEFAULT_MIN_HISTORY,
    window: int = DEFAULT_WINDOW,
) -> dict[str, Any]:
    if not 0.0 < target_coverage < 1.0:
        raise ValueError("target_coverage must be between 0 and 1")
    if min_history < 1 or window < min_history:
        raise ValueError("window must be >= min_history >= 1")

    covered: list[bool] = []
    widths: list[float] = []
    thresholds: list[float] = []
    for index, error in enumerate(absolute_errors_pct):
        prior = absolute_errors_pct[max(0, index - window) : index]
        if len(prior) < min_history:
            continue
        threshold = _quantile_nearest_rank(prior, target_coverage)
        thresholds.append(threshold)
        covered.append(float(error) <= threshold)
        widths.append(2.0 * threshold)

    return {
        "method": "causal_rolling_absolute_error_band",
        "target_coverage": target_coverage,
        "minimum_history": min_history,
        "window": window,
        "evaluated_samples": len(covered),
        "interval_coverage": (
            round(sum(covered) / len(covered), 6) if covered else None
        ),
        "average_interval_width_pct": (
            round(sum(widths) / len(widths), 6) if widths else None
        ),
        "mean_calibration_half_width_pct": (
            round(sum(thresholds) / len(thresholds), 6) if thresholds else None
        ),
    }


def augment_optimizer_report(report: dict[str, Any]) -> dict[str, Any]:
    candidates = report.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("optimizer report is missing candidates")

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        parameters = candidate.get("parameters")
        target = 0.80
        if isinstance(parameters, dict):
            raw_target = parameters.get("target_interval_coverage", 0.80)
            if isinstance(raw_target, (int, float)) and not isinstance(
                raw_target, bool
            ):
                target = float(raw_target)

        paired = candidate.get("paired_metrics")
        by_horizon = candidate.get("by_horizon")
        if not isinstance(paired, dict) or not isinstance(by_horizon, dict):
            raise ValueError(
                f"candidate {candidate.get('name')} lacks paired horizon metrics"
            )
        paired_horizons = paired.get("by_horizon")
        if not isinstance(paired_horizons, dict):
            raise ValueError(
                f"candidate {candidate.get('name')} lacks paired horizon errors"
            )

        diagnostics: dict[str, Any] = {}
        for horizon in HORIZONS:
            errors = paired_horizons.get(horizon)
            metric = by_horizon.get(horizon)
            if not isinstance(errors, list) or not isinstance(metric, dict):
                raise ValueError(
                    f"candidate {candidate.get('name')} lacks {horizon} metrics"
                )
            numeric_errors = [float(value) for value in errors]
            diagnostic = causal_interval_diagnostic(
                numeric_errors,
                target_coverage=target,
            )
            diagnostics[horizon] = diagnostic
            if metric.get("interval_coverage") is None:
                metric["interval_coverage"] = diagnostic["interval_coverage"]
                metric["average_interval_width_pct"] = diagnostic[
                    "average_interval_width_pct"
                ]
                metric["interval_coverage_source"] = diagnostic["method"]
                metric["interval_evaluated_samples"] = diagnostic[
                    "evaluated_samples"
                ]
        candidate["interval_diagnostics"] = diagnostics

    report["champion_challenger_interval_diagnostics"] = {
        "method": "causal_rolling_absolute_error_band",
        "uses_future_outcomes": False,
        "native_metrics_take_precedence": True,
        "minimum_history": DEFAULT_MIN_HISTORY,
        "window": DEFAULT_WINDOW,
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Attach champion/challenger interval diagnostics"
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("optimizer_report.json"),
    )
    args = parser.parse_args()
    payload = json.loads(args.report.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("optimizer report must contain a JSON object")
    augmented = augment_optimizer_report(payload)
    args.report.write_text(
        json.dumps(augmented, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Added causal interval diagnostics to {args.report}")


if __name__ == "__main__":
    main()
