#!/usr/bin/env python3
"""Mixture-of-experts evaluation for regime-specialized forecasting experts.

Frozen per-origin model predictions are replayed chronologically. At each origin
the validated regime detector selects a specialist weighting configuration from a
deterministic expert catalog, while a conservative fallback expert handles unknown
or historically sparse regimes. Every weighting policy only sees matured outcomes
(target timestamp <= forecast origin), so the baseline-vs-candidate comparison is
leakage-safe.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from btc_timesfm.forecasting.adaptive_weighting import _bounded_normalize
from btc_timesfm.forecasting.correlation_weighting import correlation_aware_model_weights
from btc_timesfm.forecasting.forecast_engine import (
    ADAPTIVE_MAX_WEIGHT,
    ADAPTIVE_MIN_WEIGHT,
    TARGET_HOURS,
    static_model_weights,
)
from btc_timesfm.forecasting.statistical_significance import (
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_MIN_PAIRED_SAMPLES,
    DEFAULT_SEED,
    paired_bootstrap_comparison,
)
from btc_timesfm.data.regime_detection import transition_churn, validated_regime

REGIMES = ("range", "trending", "high_volatility")
UNKNOWN_REGIME = "unknown"
REQUIRED_FEATURES = (
    "volatility_24h_pct",
    "volatility_7d_pct",
    "volatility_6h_pct",
    "momentum_24h_pct",
    "rsi_14",
)

DEFAULT_EXPERT_HISTORY_LIMIT = 200
DEFAULT_LOW_SAMPLE_THRESHOLD = DEFAULT_MIN_PAIRED_SAMPLES
FALLBACK_BASE_REGIME = "range"

EXPERT_DEFINITIONS: dict[str, dict[str, Any]] = {
    "range": {
        "description": (
            "Sideways/mixed specialist: conservative adaptive blend, tighter weight cap "
            "for diversification, small direction reward and a persistence anchor."
        ),
        "history_limit": 160,
        "confidence": 0.80,
        "max_weight": 0.38,
        "direction_reward": 0.15,
        "persistence_fallback_boost": 0.10,
        "active": True,
    },
    "trending": {
        "description": (
            "Trend specialist: longest usable window, maximum adaptive blend and the "
            "strongest direction reward so directional models dominate clean trends."
        ),
        "history_limit": 240,
        "confidence": 0.95,
        "max_weight": ADAPTIVE_MAX_WEIGHT,
        "direction_reward": 0.30,
        "persistence_fallback_boost": 0.0,
        "active": True,
    },
    "high_volatility": {
        "description": (
            "Volatility specialist: short window because volatility expands and decays "
            "quickly, low blend, tight weight cap and a strong persistence fallback."
        ),
        "history_limit": 80,
        "confidence": 0.70,
        "max_weight": 0.30,
        "direction_reward": 0.10,
        "persistence_fallback_boost": 0.16,
        "active": True,
    },
    "fallback": {
        "description": (
            "Conservative equal-ish static allocation for unknown or sparse regimes; "
            "half equal weights, half the neutral range-regime static prior."
        ),
        "history_limit": 40,
        "confidence": 0.30,
        "max_weight": None,
        "direction_reward": 0.0,
        "persistence_fallback_boost": 0.0,
        "active": True,
    },
}


def expert_catalog() -> dict[str, dict[str, Any]]:
    """Return a deep-copied, reproducible expert catalog.

    Copying guarantees callers cannot mutate the module-level definitions, which
    keeps expert configurations reproducible across runs and reports.
    """
    return {name: dict(config) for name, config in EXPERT_DEFINITIONS.items()}


def _weighted_ensemble_price(
    current: float,
    model_predictions: dict[str, dict[str, dict[str, float]]],
    horizon: str,
    weights: dict[str, float],
) -> float:
    """Mirror production's normalized weighted geometric price ensemble."""
    active = [
        (name, weight)
        for name, weight in weights.items()
        if weight > 0 and name in model_predictions and horizon in model_predictions[name]
    ]
    if not active:
        raise ValueError("ensemble weights must contain a positive value")
    total_weight = sum(weight for _, weight in active)
    weighted_log_changes = sum(
        weight * math.log(float(model_predictions[name][horizon]["price_usd"]) / current)
        for name, weight in active
    )
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


def _normalize_weights(raw: dict[str, float], cap: float | None) -> dict[str, float]:
    """Normalize weights honoring the adaptive policy's floor and a custom cap."""
    names = list(raw)
    if not names:
        return {}
    if len(names) == 1:
        return {names[0]: 1.0}
    floor = ADAPTIVE_MIN_WEIGHT
    if cap is None:
        effective_cap = ADAPTIVE_MAX_WEIGHT
    else:
        effective_cap = min(ADAPTIVE_MAX_WEIGHT, max(float(cap), 1.0 / len(names) + 1e-6))
    return _bounded_normalize(raw, floor=floor, cap=effective_cap)


def _fallback_weights(model_names: list[str]) -> dict[str, float]:
    """Conservative equal-ish static allocation for unknown or sparse regimes."""
    names = list(model_names)
    if not names:
        return {}
    prior = static_model_weights(names, FALLBACK_BASE_REGIME)
    equal = {name: 1.0 / len(names) for name in names}
    blended = {name: 0.5 * prior[name] + 0.5 * equal[name] for name in names}
    return _normalize_weights(blended, EXPERT_DEFINITIONS["fallback"]["max_weight"])


def expert_model_weights(
    expert: str,
    model_names: list[str],
    regime: str | None,
    hour: int,
    history: list[dict[str, Any]],
    actual_by_timestamp: dict[int, float],
) -> tuple[dict[str, float], dict[str, Any]]:
    """Compute one regime-specialized expert allocation for a single horizon.

    All inputs are derived from information observable at the forecast origin, so
    the returned weights never depend on outcomes that had not matured yet.
    """
    config = EXPERT_DEFINITIONS.get(expert)
    if config is None:
        raise ValueError(f"unknown expert: {expert}")
    horizon = f"{hour}h"
    if expert == "fallback":
        weights = _fallback_weights(model_names)
        return weights, {
            "mode": "fallback_static",
            "expert": expert,
            "regime": regime,
            "horizon": horizon,
            "base_prior_regime": FALLBACK_BASE_REGIME,
            "final_weights": weights,
        }

    weights, diagnostics = correlation_aware_model_weights(
        model_names,
        regime or FALLBACK_BASE_REGIME,
        hour,
        history,
        actual_by_timestamp,
        enabled=True,
        history_limit=config["history_limit"],
        confidence=config["confidence"],
    )

    raw = dict(weights)
    direction_reward = float(config.get("direction_reward") or 0.0)
    if direction_reward > 0.0:
        for name in model_names:
            accuracy = diagnostics.get("models", {}).get(name, {}).get("direction_accuracy")
            if accuracy is None:
                continue
            raw[name] *= 1.0 + direction_reward * (float(accuracy) - 0.5) * 2.0

    persistence_boost = float(config.get("persistence_fallback_boost") or 0.0)
    if persistence_boost > 0.0 and "persistence" in raw:
        persistence_mae = diagnostics.get("persistence_mae_pct")
        complex_maes = [
            float(metric["mae_pct"])
            for name, metric in (diagnostics.get("models") or {}).items()
            if name != "persistence"
            and isinstance(metric, dict)
            and metric.get("mae_pct") is not None
        ]
        if (
            persistence_mae is not None
            and complex_maes
            and float(np.mean(complex_maes)) >= float(persistence_mae)
        ):
            raw["persistence"] += persistence_boost

    final = _normalize_weights(raw, config.get("max_weight"))
    return final, {
        **diagnostics,
        "mode": f"expert_{expert}",
        "expert": expert,
        "horizon": horizon,
        "overrides": {
            "history_limit": config["history_limit"],
            "confidence": config["confidence"],
            "max_weight": config.get("max_weight"),
            "direction_reward": direction_reward,
            "persistence_fallback_boost": persistence_boost,
        },
        "final_weights": final,
    }


def _detect_regime(features: dict[str, Any]) -> str | None:
    """Classify a feature row, returning None for sparse/unknown features.

    The validated detector is deterministic but silently defaults missing inputs;
    incomplete feature rows are treated as an unknown regime and routed to the
    conservative fallback expert instead.
    """
    if not isinstance(features, dict) or not features:
        return None
    if not all(name in features for name in REQUIRED_FEATURES):
        return None
    return validated_regime(features)


def choose_expert(regime: str | None, sparse_history: bool) -> str:
    """Route an origin to a regime specialist or to the fallback expert."""
    if regime is None:
        return "fallback"
    if sparse_history:
        return "fallback"
    return regime


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


def _compare_metrics(
    candidate_scores: list[dict[str, float]],
    baseline_scores: list[dict[str, float]],
    *,
    low_sample_threshold: int,
    iterations: int,
    min_samples: int,
    seed: int,
) -> dict[str, Any]:
    """Run paired bootstrap comparisons for MAE and direction accuracy."""
    result: dict[str, Any] = {}
    score_keys = {"mae_pct": "absolute_error_pct", "direction_accuracy": "direction_correct"}
    for metric, lower_is_better in (("mae_pct", True), ("direction_accuracy", False)):
        score_key = score_keys[metric]
        candidate = [float(item[score_key]) for item in candidate_scores]
        baseline = [float(item[score_key]) for item in baseline_scores]
        comparison = paired_bootstrap_comparison(
            candidate,
            baseline,
            metric=metric,
            lower_is_better=lower_is_better,
            iterations=iterations,
            min_samples=min_samples,
            seed=seed,
        )
        result[metric] = {**comparison, "low_sample": len(candidate) < low_sample_threshold}
    return result


def _segment(
    candidate_scores: list[dict[str, float]],
    baseline_scores: list[dict[str, float]],
    *,
    low_sample_threshold: int,
    iterations: int,
    min_samples: int,
    seed: int,
) -> dict[str, Any]:
    """Build one comparison segment (per horizon / per regime-and-horizon)."""
    candidate_metrics = _aggregate(candidate_scores)
    baseline_metrics = _aggregate(baseline_scores)
    statistical = _compare_metrics(
        candidate_scores,
        baseline_scores,
        low_sample_threshold=low_sample_threshold,
        iterations=iterations,
        min_samples=min_samples,
        seed=seed,
    )
    candidate_mae = candidate_metrics["mae_pct"]
    baseline_mae = baseline_metrics["mae_pct"]
    delta: float | None = None
    if candidate_mae is not None and baseline_mae is not None:
        delta = round(float(candidate_mae) - float(baseline_mae), 6)
    low_sample = len(candidate_scores) < low_sample_threshold
    return {
        "samples": len(candidate_scores),
        "baseline": baseline_metrics,
        "candidate": candidate_metrics,
        "candidate_minus_baseline_mae_pct": delta,
        "low_sample": low_sample,
        "inconclusive": low_sample or statistical["mae_pct"]["conclusion"] == "inconclusive",
        "statistical": statistical,
    }


def _classify_segment(
    segment: dict[str, Any],
    table: str,
    horizon: str,
) -> tuple[str, dict[str, Any]]:
    mae = segment["statistical"]["mae_pct"]
    if segment["low_sample"]:
        return "inconclusive", {"table": table, "horizon": horizon, "reason": "low_sample"}
    if mae["conclusion"] == "candidate_better":
        detail: dict[str, Any] = {
            "table": table,
            "horizon": horizon,
            "samples": segment["samples"],
            "mean_mae_improvement_pct": mae["mean_improvement"],
            "probability_candidate_better": mae["probability_candidate_better"],
        }
        return "candidate_better", detail
    if mae["conclusion"] == "baseline_better":
        detail = {
            "table": table,
            "horizon": horizon,
            "samples": segment["samples"],
            "mean_mae_improvement_pct": mae["mean_improvement"],
        }
        return "baseline_better", detail
    return "inconclusive", {"table": table, "horizon": horizon, "reason": mae["reason"]}


def evaluate_regime_specialists(
    samples: list[dict[str, Any]],
    actual_by_timestamp: dict[int, float],
    *,
    history_limit: int = DEFAULT_EXPERT_HISTORY_LIMIT,
    low_sample_threshold: int = DEFAULT_LOW_SAMPLE_THRESHOLD,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    bootstrap_min_samples: int = DEFAULT_MIN_PAIRED_SAMPLES,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Replay frozen forecasts through baseline and regime-specialized policies.

    The evaluation is leakage-safe: at every origin only actuals with target
    timestamp <= origin are passed to the weighting policies, scored outcomes use
    the matured actual for each horizon, and each specialist's usable window is
    capped by its own ``history_limit``.
    """
    if low_sample_threshold < 1:
        raise ValueError("low_sample_threshold must be positive")
    if history_limit < 1:
        raise ValueError("history_limit must be positive")

    ordered = sorted(samples, key=lambda item: int(item["origin_timestamp"]))
    horizons = [f"{hour}h" for hour in TARGET_HOURS]
    history_cap = max(
        [history_limit] + [int(config["history_limit"]) for config in EXPERT_DEFINITIONS.values()]
    )

    baseline_scores: dict[str, list[dict[str, float]]] = {horizon: [] for horizon in horizons}
    candidate_scores: dict[str, list[dict[str, float]]] = {horizon: [] for horizon in horizons}
    baseline_by_regime: dict[str, dict[str, list[dict[str, float]]]] = {
        **{regime: {horizon: [] for horizon in horizons} for regime in REGIMES},
        UNKNOWN_REGIME: {horizon: [] for horizon in horizons},
    }
    candidate_by_regime: dict[str, dict[str, list[dict[str, float]]]] = {
        regime: {horizon: [] for horizon in horizons} for regime in baseline_by_regime
    }

    histories: list[dict[str, Any]] = []
    chosen_experts: list[str] = []
    fallback_reasons: list[str | None] = []

    for sample in ordered:
        timestamp = int(sample["origin_timestamp"])
        current = float(sample["current_price"])
        forecast = sample["forecast"]
        features = dict(forecast.get("market_features") or {})
        regime = _detect_regime(features)
        bucket = regime if regime is not None else UNKNOWN_REGIME
        baseline_regime = regime if regime is not None else str(forecast.get("regime") or "range")
        visible_actuals = {
            target: value for target, value in actual_by_timestamp.items() if target <= timestamp
        }
        model_predictions = forecast["model_predictions"]
        model_names = list(model_predictions)

        baseline_weights_by_h: dict[str, dict[str, float]] = {}
        baseline_sources: list[str | None] = []
        for hour in TARGET_HOURS:
            horizon = f"{hour}h"
            weights, diagnostics = correlation_aware_model_weights(
                model_names,
                baseline_regime,
                hour,
                histories,
                visible_actuals,
                history_limit=history_limit,
            )
            baseline_weights_by_h[horizon] = weights
            baseline_sources.append(diagnostics.get("source"))

        sparse_history = any(source == "insufficient_history" for source in baseline_sources)
        chosen = choose_expert(regime, sparse_history)
        chosen_experts.append(chosen)
        if chosen != "fallback":
            fallback_reasons.append(None)
        else:
            fallback_reasons.append("unknown_regime" if regime is None else "sparse_history")

        candidate_weights_by_h: dict[str, dict[str, float]] = {}
        for hour in TARGET_HOURS:
            weights, _ = expert_model_weights(
                chosen,
                model_names,
                regime,
                hour,
                histories,
                visible_actuals,
            )
            candidate_weights_by_h[f"{hour}h"] = weights

        for hour in TARGET_HOURS:
            horizon = f"{hour}h"
            actual_value = sample.get("actuals", {}).get(horizon)
            if actual_value is None:
                continue
            actual = float(actual_value)
            baseline_predicted = _weighted_ensemble_price(
                current, model_predictions, horizon, baseline_weights_by_h[horizon]
            )
            candidate_predicted = _weighted_ensemble_price(
                current, model_predictions, horizon, candidate_weights_by_h[horizon]
            )
            baseline_score = _score(current, baseline_predicted, actual)
            candidate_score = _score(current, candidate_predicted, actual)
            baseline_scores[horizon].append(baseline_score)
            candidate_scores[horizon].append(candidate_score)
            baseline_by_regime[bucket][horizon].append(baseline_score)
            candidate_by_regime[bucket][horizon].append(candidate_score)

        histories.append(_snapshot(sample, bucket))
        histories = histories[-history_cap:]

    by_horizon = {
        horizon: _segment(
            candidate_scores[horizon],
            baseline_scores[horizon],
            low_sample_threshold=low_sample_threshold,
            iterations=bootstrap_iterations,
            min_samples=bootstrap_min_samples,
            seed=seed,
        )
        for horizon in horizons
    }
    by_regime = {
        regime: {
            horizon: _segment(
                candidate_by_regime[regime][horizon],
                baseline_by_regime[regime][horizon],
                low_sample_threshold=low_sample_threshold,
                iterations=bootstrap_iterations,
                min_samples=bootstrap_min_samples,
                seed=seed,
            )
            for horizon in horizons
        }
        for regime in baseline_by_regime
    }

    promotion_candidates: list[dict[str, Any]] = []
    regressions: list[dict[str, Any]] = []
    inconclusive_segments: list[dict[str, Any]] = []
    for table, table_segments in (
        ("overall", by_horizon),
        *[(regime, by_regime[regime]) for regime in baseline_by_regime],
    ):
        for horizon, segment in table_segments.items():
            category, detail = _classify_segment(segment, table, horizon)
            if category == "candidate_better":
                promotion_candidates.append(detail)
            elif category == "baseline_better":
                regressions.append(detail)
            else:
                inconclusive_segments.append(detail)

    return {
        "experts": expert_catalog(),
        "configuration": {
            "history_limit": history_limit,
            "low_sample_threshold": low_sample_threshold,
            "bootstrap_iterations": bootstrap_iterations,
            "bootstrap_min_samples": bootstrap_min_samples,
            "seed": seed,
            "regimes": list(REGIMES),
        },
        "samples": len(ordered),
        "by_horizon": by_horizon,
        "by_regime": by_regime,
        "transition_churn": transition_churn(chosen_experts),
        "expert_utilization": {
            name: count for name, count in dict(Counter(chosen_experts)).items()
        },
        "fallback_usage": {
            "occurrences": sum(1 for reason in fallback_reasons if reason is not None),
            "reasons": {
                "unknown_regime": sum(reason == "unknown_regime" for reason in fallback_reasons),
                "sparse_history": sum(reason == "sparse_history" for reason in fallback_reasons),
            },
            "origins": [
                int(sample["origin_timestamp"])
                for sample, reason in zip(ordered, fallback_reasons, strict=True)
                if reason is not None
            ],
        },
        "chosen_expert_sequence": chosen_experts,
        "fallback_reason_sequence": fallback_reasons,
        "promotion_candidates": promotion_candidates,
        "regressions": regressions,
        "inconclusive_segments": inconclusive_segments,
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _render_segment_table(
    title: str,
    segments: dict[str, Any],
) -> list[str]:
    lines = [
        f"## {title}",
        "",
        "| Segment | Samples | Baseline MAE% | Candidate MAE% | Δ MAE% | Baseline dir | "
        "Candidate dir | Verdict |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for name, segment in segments.items():
        verdict = "inconclusive"
        mae = segment["statistical"]["mae_pct"]
        if not segment["low_sample"]:
            verdict = mae["conclusion"]
        lines.append(
            "| "
            + " | ".join(
                [
                    str(name),
                    str(segment["samples"]),
                    _fmt(segment["baseline"]["mae_pct"]),
                    _fmt(segment["candidate"]["mae_pct"]),
                    _fmt(segment["candidate_minus_baseline_mae_pct"]),
                    _fmt(segment["baseline"]["direction_accuracy"]),
                    _fmt(segment["candidate"]["direction_accuracy"]),
                    verdict,
                ]
            )
            + " |"
        )
    return lines


def render_markdown(report: dict[str, Any]) -> str:
    """Render the machine-readable report as a concise markdown summary."""
    configuration = report["configuration"]
    lines = [
        "# Regime-specialized forecasting experts",
        "",
        f"Evaluated samples: **{report['samples']}**",
        f"History limit: **{configuration['history_limit']}** | "
        f"Low-sample threshold: **{configuration['low_sample_threshold']}** | "
        f"Bootstrap iterations: **{configuration['bootstrap_iterations']}** (seed "
        f"{configuration['seed']})",
        "",
        "Each origin is scored twice: the current regime-conditioned adaptive "
        "weighting (baseline) and the regime-specialized expert chosen by the "
        "validated regime detector (candidate). Only outcomes matured at the "
        "origin are visible to either policy.",
        "",
        "## Experts",
        "",
        "| Expert | Description | History | Confidence | Max weight | Direction reward | "
        "Persistence boost |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for name, config in sorted(report["experts"].items()):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(name),
                    str(config["description"]),
                    str(config["history_limit"]),
                    _fmt(config["confidence"]),
                    _fmt(config["max_weight"]),
                    _fmt(config["direction_reward"]),
                    _fmt(config["persistence_fallback_boost"]),
                ]
            )
            + " |"
        )
    lines.extend(_render_segment_table("By horizon", report["by_horizon"]))
    for regime, segments in report["by_regime"].items():
        lines.extend(_render_segment_table(f"By regime: {regime}", segments))

    churn = report["transition_churn"]
    lines.extend(
        [
            "## Expert transitions",
            "",
            f"Transitions: **{churn['transitions']}** / {churn['samples']} samples "
            f"(rate **{churn['transition_rate']:.4f}**)",
            "",
            "| Expert | Uses |",
            "| --- | ---: |",
        ]
    )
    for name, count in sorted(report["expert_utilization"].items(), key=lambda item: item[0]):
        lines.append(f"| {name} | {count} |")

    fallback_usage = report["fallback_usage"]
    lines.extend(
        [
            "",
            "## Fallback usage",
            "",
            "| Reason | Uses |",
            "| --- | ---: |",
        ]
    )
    for reason, count in sorted(fallback_usage["reasons"].items()):
        lines.append(f"| {reason} | {count} |")
    lines.append(f"| total | {fallback_usage['occurrences']} |")

    promotion = report["promotion_candidates"]
    regression = report["regressions"]
    inconclusive = report["inconclusive_segments"]
    lines.extend(
        [
            "",
            "## Promotion review",
            "",
            f"Segments requiring promotion review (statistically defensible "
            f"out-of-sample improvement): **{len(promotion)}**",
            f"Segments regressing versus baseline: **{len(regression)}**",
            f"Segments inconclusive (low sample / interval crosses zero): **{len(inconclusive)}**",
        ]
    )
    for item in promotion:
        lines.append(
            f"- `{item['table']}` {item['horizon']}: candidate better "
            f"(n={item['samples']}, boost {_fmt(item['mean_mae_improvement_pct'])} MAE% "
            f"pts, p_better {_fmt(item['probability_candidate_better'])})"
        )
    for item in regression:
        lines.append(
            f"- `{item['table']}` {item['horizon']}: baseline better (n={item['samples']})"
        )
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare regime-conditioned weighting vs regime-specialized experts"
    )
    parser.add_argument("--samples-json", type=Path, required=True)
    parser.add_argument("--actuals-json", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("regime_specialists_report.json"))
    parser.add_argument("--markdown", type=Path, default=Path("regime_specialists_report.md"))
    parser.add_argument("--history-limit", type=int, default=DEFAULT_EXPERT_HISTORY_LIMIT)
    parser.add_argument("--low-sample-threshold", type=int, default=DEFAULT_LOW_SAMPLE_THRESHOLD)
    args = parser.parse_args()

    samples = json.loads(args.samples_json.read_text(encoding="utf-8"))
    raw_actuals = json.loads(args.actuals_json.read_text(encoding="utf-8"))
    if not isinstance(samples, list) or not isinstance(raw_actuals, dict):
        raise ValueError("samples must be a list and actuals must be an object")
    actuals = {int(key): float(value) for key, value in raw_actuals.items()}
    report = evaluate_regime_specialists(
        [item for item in samples if isinstance(item, dict)],
        actuals,
        history_limit=args.history_limit,
        low_sample_threshold=args.low_sample_threshold,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.markdown.write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "samples": report["samples"],
                "churn_transition_rate": report["transition_churn"]["transition_rate"],
                "fallback_occurrences": report["fallback_usage"]["occurrences"],
                "promotion_candidates": len(report["promotion_candidates"]),
                "regressions": len(report["regressions"]),
                "inconclusive_segments": len(report["inconclusive_segments"]),
                "json": str(args.output),
                "markdown": str(args.markdown),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
