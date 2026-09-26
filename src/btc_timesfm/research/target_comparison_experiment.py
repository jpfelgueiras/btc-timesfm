#!/usr/bin/env python3
"""Target-alignment and scoring utilities for roadmap-v5 issue #264.

This module deliberately does not silently turn a transformed incumbent forecast
into a new target model.  The production forecast is an hourly log-return path;
price and log-price results below are *round-trip diagnostics* of that forecast.
The direct-horizon candidate is marked unavailable until a separately trained
head is supplied.  This makes a blocked/inconclusive experiment explicit rather
than manufacturing evidence from identical predictions.

The functions are small and pure so a frozen, immutable dataset runner can use
them with the existing nested walk-forward machinery in ``backtest.py``.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np

TARGETS = (
    "hourly_log_return_accumulation",
    "normalized_price_level",
    "log_price_level",
    "direct_cumulative_log_return",
)
HORIZONS = ("2h", "4h", "8h", "16h")
REPORT_PATH = Path("target_comparison_report.json")


def accumulate_return_path(origin_price: float, hourly_returns: Iterable[float]) -> np.ndarray:
    """Reconstruct prices from an hourly log-return path, causally from the origin."""
    if not math.isfinite(origin_price) or origin_price <= 0:
        raise ValueError("origin_price must be finite and positive")
    returns = np.asarray(list(hourly_returns), dtype=np.float64)
    if returns.ndim != 1 or not np.all(np.isfinite(returns)):
        raise ValueError("hourly_returns must be a finite one-dimensional path")
    return origin_price * np.exp(np.cumsum(returns))


def cumulative_quantile_paths(
    origin_price: float, hourly_quantiles: Mapping[str, Iterable[float]]
) -> dict[str, np.ndarray]:
    """Accumulate each supplied marginal quantile path independently.

    These are marginal-path diagnostics, not mathematically calibrated quantiles
    of cumulative returns: marginal hourly quantiles do not define their joint law.
    """
    expected = {"q10", "q50", "q90"}
    if set(hourly_quantiles) != expected:
        raise ValueError("hourly_quantiles must contain exactly q10, q50, and q90")
    paths = {
        name: accumulate_return_path(origin_price, hourly_quantiles[name])
        for name in sorted(expected)
    }
    if any(len(paths[name]) != len(paths["q50"]) for name in expected):
        raise ValueError("quantile paths must have equal lengths")
    if np.any(paths["q10"] > paths["q50"]) or np.any(paths["q50"] > paths["q90"]):
        raise ValueError("hourly quantile paths cross after accumulation")
    return paths


def target_value(price: float, origin_price: float, target: str) -> float:
    """Return the matured label represented by ``target`` at one horizon."""
    if price <= 0 or origin_price <= 0:
        raise ValueError("prices must be positive")
    if target in {"hourly_log_return_accumulation", "direct_cumulative_log_return"}:
        return math.log(price / origin_price)
    if target == "normalized_price_level":
        return price / origin_price
    if target == "log_price_level":
        return math.log(price)
    raise ValueError(f"unknown target: {target}")


def invert_target(value: float, origin_price: float, target: str) -> float:
    """Invert a target prediction to USD, using only the origin context."""
    if origin_price <= 0:
        raise ValueError("origin_price must be positive")
    if target in {"hourly_log_return_accumulation", "direct_cumulative_log_return"}:
        return origin_price * math.exp(value)
    if target == "normalized_price_level":
        return origin_price * value
    if target == "log_price_level":
        return math.exp(value)
    raise ValueError(f"unknown target: {target}")


def round_trip(price: float, origin_price: float, target: str) -> float:
    """Check the label transform/inversion used by the experiment."""
    return invert_target(target_value(price, origin_price, target), origin_price, target)


def _prediction_for_target(predicted_price: float, origin_price: float, target: str) -> float:
    return target_value(predicted_price, origin_price, target)


def _loss(
    predicted_price: float, actual_price: float, origin_price: float, target: str
) -> dict[str, float]:
    predicted = _prediction_for_target(predicted_price, origin_price, target)
    actual = target_value(actual_price, origin_price, target)
    inverted = invert_target(predicted, origin_price, target)
    return {
        "target_prediction": predicted,
        "target_actual": actual,
        "return_abs_error": abs(
            math.log(predicted_price / origin_price) - math.log(actual_price / origin_price)
        ),
        "price_ape_pct": abs(inverted - actual_price) / actual_price * 100.0,
        "signed_return_error": math.log(predicted_price / origin_price)
        - math.log(actual_price / origin_price),
    }


def evaluate_origins(origins: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Score paired origins without dropping failed/missing forecasts.

    Each origin must contain ``origin_price`` and, per horizon, ``actual_price``
    and ``predicted_price``.  Optional ``fold`` and ``regime`` fields are
    retained in the machine-readable per-origin ledger.
    """
    ledger: list[dict[str, Any]] = []
    totals: dict[str, dict[str, dict[str, list[float]]]] = {
        horizon: {target: {"return_abs_error": [], "price_ape_pct": []} for target in TARGETS}
        for horizon in HORIZONS
    }
    failures: list[dict[str, Any]] = []
    for number, origin in enumerate(origins):
        origin_price = float(origin["origin_price"])
        row: dict[str, Any] = {
            "origin": origin.get("origin"),
            "fold": origin.get("fold"),
            "regime": origin.get("regime"),
            "horizons": {},
        }
        for horizon in HORIZONS:
            try:
                item = origin[horizon]
                actual = float(item["actual_price"])
                predicted = float(item["predicted_price"])
                if not (math.isfinite(actual) and math.isfinite(predicted)):
                    raise ValueError("non-finite forecast or label")
                row["horizons"][horizon] = {
                    target: _loss(predicted, actual, origin_price, target) for target in TARGETS
                }
                for target in TARGETS:
                    scored = row["horizons"][horizon][target]
                    totals[horizon][target]["return_abs_error"].append(scored["return_abs_error"])
                    totals[horizon][target]["price_ape_pct"].append(scored["price_ape_pct"])
            except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
                row["horizons"][horizon] = {"status": "missing", "error": str(exc)}
                failures.append({"origin_index": number, "horizon": horizon, "error": str(exc)})
        ledger.append(row)

    summary: dict[str, Any] = {}
    for horizon in HORIZONS:
        summary[horizon] = {}
        for target in TARGETS:
            values = totals[horizon][target]
            summary[horizon][target] = {
                "samples": len(values["return_abs_error"]),
                "return_mae": float(np.mean(values["return_abs_error"]))
                if values["return_abs_error"]
                else None,
                "price_mape_pct": float(np.mean(values["price_ape_pct"]))
                if values["price_ape_pct"]
                else None,
            }
    return {
        "kind": "incumbent_roundtrip_diagnostics_not_candidate_evidence",
        "per_origin": ledger,
        "summary": summary,
        "failures": failures,
    }


def decide(report: Mapping[str, Any]) -> dict[str, Any]:
    """Apply prerequisite and conservative V5 gates; proxies never qualify."""
    manifest = report.get("manifest", {})
    blockers = []
    if not manifest.get("eligible_corpus", False):
        blockers.append("eligible immutable benchmark corpus unavailable")
    if not manifest.get("direct_head_available", False):
        blockers.append("no supported independently supervised direct-target head")
    if blockers:
        return {
            "decision": "blocked",
            "reason": "; ".join(blockers),
            "promotion_eligible": False,
            "required_evidence": [
                "validated immutable BTC/USD corpus passing the canonical benchmark gate",
                "supported independently trained predictions for each candidate target",
                "nested purged walk-forward folds, matured labels, and paired dependence-aware intervals",
            ],
        }
    return {
        "decision": "inconclusive",
        "reason": "direct target head unavailable; transformed incumbent views are diagnostics, not candidates",
        "promotion_eligible": False,
        "required_evidence": [
            "same-origin direct-head predictions",
            "nested purged folds with matured labels",
            "paired dependence-aware confidence intervals",
            "immutable dataset and runtime manifest",
        ],
    }


def build_report(
    origins: Iterable[Mapping[str, Any]], manifest: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    evidence = evaluate_origins(origins)
    experiment_manifest = {
        "eligible_corpus": False,
        "direct_head_available": False,
        "direct_head_status": "unavailable_no_supported_training_or_inference_contract",
        "production_baseline": "frozen hourly log-return path and persistence",
        **dict(manifest or {}),
    }
    report: dict[str, Any] = {
        "schema_version": 1,
        "targets": list(TARGETS),
        "horizons": list(HORIZONS),
        "manifest": experiment_manifest,
        "evidence": evidence,
        "candidate_status": {
            "hourly_log_return_accumulation": "incumbent_diagnostic_only",
            "normalized_price_level": "blocked_no_independent_head",
            "log_price_level": "blocked_no_independent_head",
            "direct_cumulative_log_return": "blocked_no_independent_head",
        },
        "quantile_semantics": (
            "Accumulated q10/q50/q90 hourly marginals are not calibrated cumulative-return "
            "quantiles without a joint predictive distribution."
        ),
    }
    report["decision"] = decide(report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="JSON origin ledger")
    parser.add_argument("--output", type=Path, default=REPORT_PATH)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text())
    origins = payload["origins"] if isinstance(payload, dict) and "origins" in payload else payload
    report = build_report(origins, payload.get("manifest", {}) if isinstance(payload, dict) else {})
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report["decision"], sort_keys=True))


if __name__ == "__main__":
    main()
