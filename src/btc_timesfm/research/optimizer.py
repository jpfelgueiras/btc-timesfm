#!/usr/bin/env python3
"""Bounded weekly walk-forward optimizer for the BTC forecast ensemble.

The expensive TimesFM model forecasts are generated once per historical origin.
Candidate adaptive-weighting configurations are then replayed over those frozen
model forecasts, so the candidate search stays cheap and deterministic. At each
simulated origin, adaptive weights may only use outcomes whose target candle is
already visible at that origin.

This first version is recommendation-only: it writes a report and never changes
production parameters or opens a configuration PR automatically.
"""

from __future__ import annotations

import argparse
import json
import math
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from btc_timesfm.forecasting import adaptive_weighting as aw
from btc_timesfm.research.backtest import fetch_binance_history, slice_market
from btc_timesfm.forecasting.statistical_significance import (
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_CONFIDENCE,
    DEFAULT_MIN_PAIRED_SAMPLES,
    paired_bootstrap_comparison,
)
from btc_timesfm.forecasting.forecast_engine import (
    TARGET_HOURS,
    baseline_forecasts,
    detect_regime,
    load_timesfm,
    market_features,
    timesfm_multi_context,
)


REPORT_PATH = Path("optimizer_report.json")
SUMMARY_PATH = Path("optimizer_summary.md")
DEFAULT_DAYS = 120
DEFAULT_SAMPLES = 48
MIN_RECOMMENDATION_SAMPLES = 32
MIN_RELATIVE_MAE_IMPROVEMENT = 0.03
MAX_HORIZON_RELATIVE_DEGRADATION = 0.05
MAX_DIRECTION_ACCURACY_DROP = 0.02
MAX_WORST_FOLD_RELATIVE_DEGRADATION = 0.02
MIN_IMPROVED_FOLDS = 2
HORIZONS = ("2h", "4h", "8h", "16h")

ALL_MODELS = (
    "timesfm_168h",
    "timesfm_336h",
    "timesfm_512h",
    "persistence",
    "drift_7d",
    "ar1",
)


@dataclass(frozen=True)
class CandidateConfig:
    name: str
    enabled_models: tuple[str, ...] = ALL_MODELS
    min_samples: int = 6
    full_samples: int = 24
    max_blend: float = 0.80
    min_weight: float = 0.03
    max_weight: float = 0.55
    mae_lambda: float = 2.5
    direction_reward: float = 0.25
    persistence_boost: float = 0.12
    history_limit: int = 200
    target_interval_coverage: float = 0.80
    coverage_penalty: float = 0.35


def production_config() -> CandidateConfig:
    """Return the current deployed issue-#6 policy."""
    return CandidateConfig(name="production")


def candidate_catalog() -> list[CandidateConfig]:
    """Small deterministic search space with one-factor and conservative variants."""
    current = production_config()
    return [
        current,
        CandidateConfig(name="longer_history", history_limit=300),
        CandidateConfig(name="shorter_history", history_limit=120),
        CandidateConfig(name="more_samples_before_adapt", min_samples=10, full_samples=36),
        CandidateConfig(name="faster_adaptation", min_samples=6, full_samples=18, max_blend=0.85),
        CandidateConfig(name="slower_adaptation", min_samples=8, full_samples=32, max_blend=0.70),
        CandidateConfig(name="mae_more_sensitive", mae_lambda=3.25),
        CandidateConfig(name="mae_less_sensitive", mae_lambda=1.75),
        CandidateConfig(name="direction_more_weight", direction_reward=0.35),
        CandidateConfig(name="tighter_weight_bounds", min_weight=0.05, max_weight=0.45),
        CandidateConfig(name="stronger_persistence_fallback", persistence_boost=0.20),
        CandidateConfig(
            name="long_contexts_only",
            enabled_models=("timesfm_336h", "timesfm_512h", "persistence", "drift_7d", "ar1"),
        ),
        CandidateConfig(
            name="timesfm_512_only",
            enabled_models=("timesfm_512h", "persistence", "drift_7d", "ar1"),
            max_weight=0.65,
        ),
        CandidateConfig(
            name="interval_stricter", target_interval_coverage=0.80, coverage_penalty=0.60
        ),
    ]


@contextmanager
def apply_candidate(config: CandidateConfig) -> Iterator[None]:
    """Temporarily install candidate hyperparameters in adaptive_weighting.

    The production module remains unchanged after the candidate is evaluated.
    This keeps optimizer experiments isolated from the scheduled forecast path.
    """
    names = {
        "ADAPTIVE_MIN_SAMPLES": config.min_samples,
        "ADAPTIVE_FULL_SAMPLES": config.full_samples,
        "ADAPTIVE_MAX_BLEND": config.max_blend,
        "ADAPTIVE_MIN_WEIGHT": config.min_weight,
        "ADAPTIVE_MAX_WEIGHT": config.max_weight,
        "ADAPTIVE_MAE_LAMBDA": config.mae_lambda,
        "ADAPTIVE_DIRECTION_REWARD": config.direction_reward,
        "PERSISTENCE_FALLBACK_BOOST": config.persistence_boost,
        "TARGET_INTERVAL_COVERAGE": config.target_interval_coverage,
        "COVERAGE_PENALTY": config.coverage_penalty,
    }
    original = {name: getattr(aw, name) for name in names}
    try:
        for name, value in names.items():
            setattr(aw, name, value)
        yield
    finally:
        for name, value in original.items():
            setattr(aw, name, value)


def _direction(value: float, epsilon: float = 1e-9) -> int:
    return 1 if value > epsilon else -1 if value < -epsilon else 0


def _score(current: float, predicted: float, actual: float) -> dict[str, Any]:
    error = predicted - actual
    return {
        "absolute_error_pct": abs(error) / actual * 100.0,
        "signed_error_pct": error / actual * 100.0,
        "direction_correct": _direction(predicted - current) == _direction(actual - current),
    }


def _ensemble_price(
    current: float,
    model_predictions: dict[str, dict[str, dict[str, float]]],
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


def _history_snapshot(sample: dict[str, Any]) -> dict[str, Any]:
    return {
        "latest_close_at": sample["origin_at"],
        "latest_close_usd": sample["current_price"],
        "regime": sample["regime"],
        "model_predictions": sample["model_predictions"],
        "predictions": {},
        "model_weights": {},
    }


def generate_base_samples(days: int, samples: int) -> tuple[list[dict[str, Any]], dict[int, float]]:
    """Run TimesFM once per origin and retain frozen per-model predictions."""
    data = fetch_binance_history(days)
    first = 513
    last = len(data.closes) - max(TARGET_HOURS) - 1
    if last <= first:
        raise RuntimeError("Historical window is too small for optimizer")

    requested = min(max(1, samples), last - first + 1)
    indices = sorted(set(map(int, np.linspace(first, last, num=requested, dtype=int))))
    model = load_timesfm()
    result: list[dict[str, Any]] = []

    for number, index in enumerate(indices, start=1):
        context = slice_market(data, index)
        features = market_features(context)
        regime = detect_regime(features)
        model_predictions = {
            **timesfm_multi_context(model, context),
            **baseline_forecasts(context),
        }
        result.append(
            {
                "index": index,
                "origin_at": datetime.fromtimestamp(
                    data.timestamps[index], tz=timezone.utc
                ).isoformat(),
                "origin_timestamp": int(data.timestamps[index]),
                "current_price": float(data.closes[index]),
                "regime": regime,
                "model_predictions": model_predictions,
                "actuals": {f"{hour}h": float(data.closes[index + hour]) for hour in TARGET_HOURS},
            }
        )
        print(f"Base forecast {number}/{len(indices)}: {result[-1]['origin_at']} ({regime})")

    actual_by_timestamp = dict(zip(data.timestamps, map(float, data.closes), strict=True))
    return result, actual_by_timestamp


def replay_candidate(
    config: CandidateConfig,
    samples: list[dict[str, Any]],
    all_actuals: dict[int, float],
) -> dict[str, Any]:
    """Replay one configuration with strict origin-time outcome visibility."""
    history: list[dict[str, Any]] = []
    scored: list[dict[str, Any]] = []

    with apply_candidate(config):
        for sample in samples:
            current_timestamp = int(sample["origin_timestamp"])
            visible_actuals = {
                timestamp: price
                for timestamp, price in all_actuals.items()
                if timestamp <= current_timestamp
            }
            available_models = [
                name for name in config.enabled_models if name in sample["model_predictions"]
            ]
            if "persistence" not in available_models:
                raise RuntimeError(f"Candidate {config.name} must include persistence")

            horizon_results: dict[str, Any] = {}
            for hour in TARGET_HOURS:
                horizon = f"{hour}h"
                weights, diagnostics = aw.adaptive_model_weights(
                    available_models,
                    sample["regime"],
                    hour,
                    history,
                    visible_actuals,
                    history_limit=config.history_limit,
                )
                predicted = _ensemble_price(
                    float(sample["current_price"]),
                    sample["model_predictions"],
                    horizon,
                    weights,
                )
                score = _score(
                    float(sample["current_price"]),
                    predicted,
                    float(sample["actuals"][horizon]),
                )
                horizon_results[horizon] = {
                    **score,
                    "predicted_price_usd": predicted,
                    "actual_price_usd": float(sample["actuals"][horizon]),
                    "weighting_mode": diagnostics["mode"],
                }

            persistence = {
                horizon: _score(
                    float(sample["current_price"]),
                    float(sample["current_price"]),
                    float(sample["actuals"][horizon]),
                )
                for horizon in ("2h", "4h", "8h", "16h")
            }
            scored.append(
                {
                    "origin_at": sample["origin_at"],
                    "regime": sample["regime"],
                    "horizons": horizon_results,
                    "persistence": persistence,
                }
            )
            history.append(_history_snapshot(sample))

    return summarize_candidate(config, scored)


def _aggregate(scores: list[dict[str, Any]]) -> dict[str, Any]:
    if not scores:
        return {
            "samples": 0,
            "mae_pct": None,
            "mean_signed_error_pct": None,
            "direction_accuracy": None,
        }
    return {
        "samples": len(scores),
        "mae_pct": round(float(np.mean([s["absolute_error_pct"] for s in scores])), 6),
        "mean_signed_error_pct": round(float(np.mean([s["signed_error_pct"] for s in scores])), 6),
        "direction_accuracy": round(float(np.mean([s["direction_correct"] for s in scores])), 6),
    }


def _fold_indices(length: int, folds: int = 3) -> list[tuple[int, int]]:
    if length <= 0:
        return []
    chunks = np.array_split(np.arange(length), min(folds, length))
    return [(int(chunk[0]), int(chunk[-1]) + 1) for chunk in chunks if len(chunk)]


def summarize_candidate(config: CandidateConfig, scored: list[dict[str, Any]]) -> dict[str, Any]:
    by_horizon: dict[str, Any] = {}
    persistence_by_horizon: dict[str, Any] = {}
    by_regime: dict[str, Any] = {}

    for horizon in ("2h", "4h", "8h", "16h"):
        by_horizon[horizon] = _aggregate([item["horizons"][horizon] for item in scored])
        persistence_by_horizon[horizon] = _aggregate(
            [item["persistence"][horizon] for item in scored]
        )

    for regime in sorted({item["regime"] for item in scored}):
        by_regime[regime] = {
            horizon: _aggregate(
                [item["horizons"][horizon] for item in scored if item["regime"] == regime]
            )
            for horizon in ("2h", "4h", "8h", "16h")
        }

    folds: list[dict[str, Any]] = []
    for number, (start, end) in enumerate(_fold_indices(len(scored)), start=1):
        fold_scores = [
            item["horizons"][horizon]
            for item in scored[start:end]
            for horizon in ("2h", "4h", "8h", "16h")
        ]
        metric = _aggregate(fold_scores)
        folds.append({"fold": number, "start": start, "end": end, **metric})

    horizon_maes = [
        float(value["mae_pct"]) for value in by_horizon.values() if value["mae_pct"] is not None
    ]
    horizon_dirs = [
        float(value["direction_accuracy"])
        for value in by_horizon.values()
        if value["direction_accuracy"] is not None
    ]
    objective_mae = float(np.mean(horizon_maes)) if horizon_maes else float("inf")
    direction_accuracy = float(np.mean(horizon_dirs)) if horizon_dirs else 0.0
    paired_metrics = {
        "origins": [item["origin_at"] for item in scored],
        "mae_pct": [
            float(np.mean([item["horizons"][h]["absolute_error_pct"] for h in HORIZONS]))
            for item in scored
        ],
        "direction_accuracy": [
            float(np.mean([item["horizons"][h]["direction_correct"] for h in HORIZONS]))
            for item in scored
        ],
        "by_horizon": {
            horizon: [float(item["horizons"][horizon]["absolute_error_pct"]) for item in scored]
            for horizon in HORIZONS
        },
        "persistence_mae_pct": [
            float(np.mean([item["persistence"][h]["absolute_error_pct"] for h in HORIZONS]))
            for item in scored
        ],
    }

    return {
        "name": config.name,
        "parameters": asdict(config),
        "samples": len(scored),
        "objective_mae_pct": round(objective_mae, 6),
        "mean_direction_accuracy": round(direction_accuracy, 6),
        "by_horizon": by_horizon,
        "by_regime": by_regime,
        "persistence_by_horizon": persistence_by_horizon,
        "folds": folds,
        "paired_metrics": paired_metrics,
    }


def _significance_comparison(candidate: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    candidate_metrics = candidate["paired_metrics"]
    current_metrics = current["paired_metrics"]
    if candidate_metrics["origins"] != current_metrics["origins"]:
        raise ValueError("statistical comparisons require identical forecast origins")

    candidate_vs_production = {
        "mae_pct": paired_bootstrap_comparison(
            candidate_metrics["mae_pct"],
            current_metrics["mae_pct"],
            metric="mae_pct",
            lower_is_better=True,
        ),
        "direction_accuracy": paired_bootstrap_comparison(
            candidate_metrics["direction_accuracy"],
            current_metrics["direction_accuracy"],
            metric="direction_accuracy",
            lower_is_better=False,
        ),
        "by_horizon_mae_pct": {
            horizon: paired_bootstrap_comparison(
                candidate_metrics["by_horizon"][horizon],
                current_metrics["by_horizon"][horizon],
                metric=f"{horizon}_mae_pct",
                lower_is_better=True,
            )
            for horizon in HORIZONS
        },
    }
    candidate_vs_persistence = {
        "mae_pct": paired_bootstrap_comparison(
            candidate_metrics["mae_pct"],
            candidate_metrics["persistence_mae_pct"],
            metric="mae_pct",
            lower_is_better=True,
        )
    }
    return {
        "method": "paired_bootstrap",
        "confidence": DEFAULT_CONFIDENCE,
        "iterations": DEFAULT_BOOTSTRAP_ITERATIONS,
        "minimum_paired_samples": DEFAULT_MIN_PAIRED_SAMPLES,
        "pairing_key": "forecast_origin",
        "candidate_vs_production": candidate_vs_production,
        "candidate_vs_persistence": candidate_vs_persistence,
    }


def compare_to_current(candidate: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    current_mae = float(current["objective_mae_pct"])
    candidate_mae = float(candidate["objective_mae_pct"])
    relative_improvement = (current_mae - candidate_mae) / current_mae if current_mae > 0 else 0.0

    horizon_changes: dict[str, float] = {}
    for horizon in ("2h", "4h", "8h", "16h"):
        base = float(current["by_horizon"][horizon]["mae_pct"])
        new = float(candidate["by_horizon"][horizon]["mae_pct"])
        horizon_changes[horizon] = (base - new) / base if base > 0 else 0.0

    fold_changes: list[float] = []
    for cand_fold, current_fold in zip(candidate["folds"], current["folds"], strict=True):
        base = float(current_fold["mae_pct"])
        new = float(cand_fold["mae_pct"])
        fold_changes.append((base - new) / base if base > 0 else 0.0)

    direction_delta = float(candidate["mean_direction_accuracy"]) - float(
        current["mean_direction_accuracy"]
    )
    significance = _significance_comparison(candidate, current)
    return {
        "relative_mae_improvement": round(relative_improvement, 6),
        "direction_accuracy_delta": round(direction_delta, 6),
        "horizon_relative_improvement": {
            key: round(value, 6) for key, value in horizon_changes.items()
        },
        "fold_relative_improvement": [round(value, 6) for value in fold_changes],
        "improved_folds": sum(value > 0 for value in fold_changes),
        "worst_fold_relative_improvement": round(min(fold_changes), 6) if fold_changes else 0.0,
        "significance": significance,
    }


def recommendation(
    candidate: dict[str, Any], current: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    comparison = compare_to_current(candidate, current)
    enough_samples = int(candidate["samples"]) >= MIN_RECOMMENDATION_SAMPLES
    material_improvement = comparison["relative_mae_improvement"] >= MIN_RELATIVE_MAE_IMPROVEMENT
    horizon_safe = all(
        value >= -MAX_HORIZON_RELATIVE_DEGRADATION
        for value in comparison["horizon_relative_improvement"].values()
    )
    direction_safe = comparison["direction_accuracy_delta"] >= -MAX_DIRECTION_ACCURACY_DROP
    fold_safe = (
        comparison["improved_folds"] >= MIN_IMPROVED_FOLDS
        and comparison["worst_fold_relative_improvement"] >= -MAX_WORST_FOLD_RELATIVE_DEGRADATION
    )
    production_evidence = comparison["significance"]["candidate_vs_production"]["mae_pct"]
    persistence_evidence = comparison["significance"]["candidate_vs_persistence"]["mae_pct"]
    statistically_supported = production_evidence["conclusion"] == "candidate_better"
    persistence_safe = persistence_evidence["conclusion"] != "baseline_better"
    checks = {
        "enough_samples": enough_samples,
        "material_mae_improvement": material_improvement,
        "no_material_horizon_regression": horizon_safe,
        "direction_accuracy_not_materially_worse": direction_safe,
        "stable_across_folds": fold_safe,
        "statistically_supported_mae_improvement": statistically_supported,
        "not_significantly_worse_than_persistence": persistence_safe,
    }
    decision = "candidate_worth_review" if all(checks.values()) else "keep_current"
    return decision, {**comparison, "checks": checks}


def choose_candidate(results: list[dict[str, Any]]) -> tuple[dict[str, Any], str, dict[str, Any]]:
    current = next(result for result in results if result["name"] == "production")
    alternatives = [result for result in results if result["name"] != "production"]
    best = (
        min(alternatives, key=lambda item: float(item["objective_mae_pct"]))
        if alternatives
        else current
    )
    decision, comparison = recommendation(best, current)
    if decision == "keep_current":
        return current, decision, comparison
    return best, decision, comparison


def render_summary(report: dict[str, Any]) -> str:
    current = next(item for item in report["candidates"] if item["name"] == "production")
    selected = report["selected"]
    lines = [
        "# Weekly BTC forecast optimizer",
        "",
        f"- Recommendation: **{report['recommendation']}**",
        f"- Tested period: **{report['tested_period']['days']} days**",
        f"- Walk-forward origins: **{report['tested_period']['samples']}**",
        f"- Candidate configurations: **{len(report['candidates'])}**",
        f"- Current mean horizon MAE: **{current['objective_mae_pct']:.4f}%**",
        f"- Selected mean horizon MAE: **{selected['objective_mae_pct']:.4f}%**",
        f"- Selected configuration: **{selected['name']}**",
        "",
        "## Horizon comparison",
        "",
        "| Horizon | Current MAE | Selected MAE | Persistence MAE |",
        "| --- | ---: | ---: | ---: |",
    ]
    for horizon in ("2h", "4h", "8h", "16h"):
        lines.append(
            f"| {horizon} | {current['by_horizon'][horizon]['mae_pct']:.4f}% | "
            f"{selected['by_horizon'][horizon]['mae_pct']:.4f}% | "
            f"{current['persistence_by_horizon'][horizon]['mae_pct']:.4f}% |"
        )
    lines.extend(
        [
            "",
            "## Promotion guardrails",
            "",
        ]
    )
    for name, passed in report["comparison"]["checks"].items():
        lines.append(f"- {'✅' if passed else '❌'} `{name}`")
    lines.extend(
        [
            "",
            "This workflow is recommendation-only. It never changes production parameters automatically.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run bounded weekly walk-forward optimization")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    args = parser.parse_args()

    catalog = candidate_catalog()
    base_samples, actuals = generate_base_samples(args.days, args.samples)
    results: list[dict[str, Any]] = []
    for number, config in enumerate(catalog, start=1):
        print(f"Evaluating candidate {number}/{len(catalog)}: {config.name}")
        results.append(replay_candidate(config, base_samples, actuals))

    selected, decision, comparison = choose_candidate(results)
    report = {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_source": "Binance BTCUSDT 1h historical proxy; production uses Kraken BTC/USD",
        "tested_period": {"days": args.days, "samples": len(base_samples)},
        "search_space": {
            "candidate_count": len(catalog),
            "candidate_names": [config.name for config in catalog],
            "bounded": True,
        },
        "guardrails": {
            "minimum_samples": MIN_RECOMMENDATION_SAMPLES,
            "minimum_relative_mae_improvement": MIN_RELATIVE_MAE_IMPROVEMENT,
            "maximum_horizon_relative_degradation": MAX_HORIZON_RELATIVE_DEGRADATION,
            "maximum_direction_accuracy_drop": MAX_DIRECTION_ACCURACY_DROP,
            "minimum_improved_folds": MIN_IMPROVED_FOLDS,
            "maximum_worst_fold_relative_degradation": MAX_WORST_FOLD_RELATIVE_DEGRADATION,
            "statistical_evidence": {
                "method": "paired_bootstrap",
                "confidence": DEFAULT_CONFIDENCE,
                "iterations": DEFAULT_BOOTSTRAP_ITERATIONS,
                "minimum_paired_samples": DEFAULT_MIN_PAIRED_SAMPLES,
                "promotion_requires_candidate_better_than_production": True,
                "promotion_rejects_evidence_persistence_is_better": True,
            },
        },
        "recommendation": decision,
        "selected": selected,
        "comparison": comparison,
        "candidates": results,
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    SUMMARY_PATH.write_text(render_summary(report), encoding="utf-8")
    print(SUMMARY_PATH.read_text(encoding="utf-8"))
    print(f"Saved {REPORT_PATH} and {SUMMARY_PATH}")


if __name__ == "__main__":
    main()
