#!/usr/bin/env python3
"""Walk-forward evaluation of independent model-family candidates.

Runs ``gbdt_features`` and ``elasticnet_features`` through the identical
expanding-window origins as the existing ``ridge_features`` model and the
statistical baselines, then reports standalone skill (MAE%, direction accuracy)
by horizon and regime, residual-error correlation with the existing models, and
per-forecast runtime. Candidates are research-only: nothing here changes the
production ensemble, and the report records that no candidate is promoted from
in-sample gains alone.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

from btc_timesfm._requests import requests
from btc_timesfm.forecasting.benchmarks import benchmark_forecasts
from btc_timesfm.forecasting.diversified_model import ridge_feature_forecast
from btc_timesfm.forecasting.forecast_engine import (
    TARGET_HOURS,
    MarketData,
    detect_regime,
    market_features,
)
from btc_timesfm.research.independent_models import (
    MODEL_NAMES as CANDIDATE_MODEL_NAMES,
    elasticnet_feature_forecast,
    gbdt_feature_forecast,
)

CANDIDATE_MODELS: dict[str, Callable[..., dict[str, dict[str, float]]]] = {
    "gbdt_features": gbdt_feature_forecast,
    "elasticnet_features": elasticnet_feature_forecast,
}
COMPARISON_MODELS: dict[str, Callable[..., dict[str, dict[str, float]]]] = {
    "ridge_features": ridge_feature_forecast,
}
PRODUCTION_NOTE = (
    "Independent candidates are evaluated for standalone skill and residual "
    "diversity only. No candidate is added to production or the ensemble from "
    "in-sample gains; promotion requires the existing statistical-evidence and "
    "promotion-policy gates."
)
BINANCE_KLINES = "https://data-api.binance.vision/api/v3/klines"


def _direction(value: float, epsilon: float = 1e-9) -> int:
    return 1 if value > epsilon else -1 if value < -epsilon else 0


def _score(current: float, predicted: float, actual: float) -> dict[str, float]:
    error = predicted - actual
    return {
        "absolute_error_pct": abs(error) / actual * 100.0,
        "signed_error_pct": error / actual * 100.0,
        "direction_correct": float(_direction(predicted - current) == _direction(actual - current)),
    }


def _aggregate(scores: list[dict[str, float]]) -> dict[str, float | int | None]:
    if not scores:
        return {"samples": 0, "mae_pct": None, "direction_accuracy": None}
    return {
        "samples": len(scores),
        "mae_pct": round(float(np.mean([s["absolute_error_pct"] for s in scores])), 6),
        "direction_accuracy": round(
            float(np.mean([bool(s["direction_correct"]) for s in scores])), 6
        ),
    }


def _data_slice(data: Any, end_index: int) -> MarketData:
    """Expanding-window slice ending at ``end_index`` (inclusive)."""
    end = end_index + 1
    return MarketData(
        timestamps=[int(t) for t in np.asarray(data.timestamps[:end])],
        opens=np.asarray(data.opens[:end], dtype=float),
        highs=np.asarray(data.highs[:end], dtype=float),
        lows=np.asarray(data.lows[:end], dtype=float),
        closes=np.asarray(data.closes[:end], dtype=float),
        volumes=np.asarray(data.volumes[:end], dtype=float),
    )


def run_walk_forward_evaluation(
    data: Any,
    *,
    first_origin: int,
    last_origin: int,
    num_origins: int,
    candidates: dict[str, Callable[..., dict[str, dict[str, float]]]] | None = None,
    comparison_models: dict[str, Callable[..., dict[str, dict[str, float]]]] | None = None,
    include_benchmarks: bool = True,
) -> list[dict[str, Any]]:
    """Run every candidate through identical expanding walk-forward origins."""
    candidates = candidates or dict(CANDIDATE_MODELS)
    comparison_models = comparison_models or dict(COMPARISON_MODELS)
    closes = np.asarray(data.closes, dtype=float)
    if first_origin < 0 or last_origin > len(closes) - max(TARGET_HOURS) - 1:
        raise ValueError("origin bounds must leave matured actual horizons available")
    origin_indices = np.linspace(
        first_origin, last_origin, num=min(num_origins, last_origin - first_origin + 1), dtype=int
    )
    origins = sorted(set(map(int, origin_indices)))

    all_models = {**candidates, **comparison_models}
    samples: list[dict[str, Any]] = []
    for origin in origins:
        context = _data_slice(data, origin)
        regime = detect_regime(market_features(context))
        actuals = {f"{hour}h": float(closes[origin + hour]) for hour in TARGET_HOURS}
        models: dict[str, dict[str, dict[str, float]]] = {}
        runtimes: dict[str, float] = {}
        training_samples: dict[str, dict[str, float]] = {}
        for name, forecast_function in all_models.items():
            started = time.perf_counter()
            forecast = forecast_function(context)
            runtimes[name] = time.perf_counter() - started
            models[name] = forecast
            training_samples[name] = {
                horizon: float(item["training_samples"]) for horizon, item in forecast.items()
            }
        benchmarks = benchmark_forecasts(context) if include_benchmarks else {}
        samples.append(
            {
                "origin_timestamp": int(data.timestamps[origin]),
                "current_price": float(closes[origin]),
                "regime": regime,
                "actuals": actuals,
                "models": models,
                "benchmarks": benchmarks,
                "runtimes": runtimes,
                "training_samples": training_samples,
            }
        )
    return samples


def _scores_for_model(
    samples: list[dict[str, Any]],
    horizon: str,
    model_name: str,
) -> list[dict[str, float]]:
    scores: list[dict[str, float]] = []
    for sample in samples:
        predicted = float(sample["models"][model_name][horizon]["price_usd"])
        scores.append(_score(sample["current_price"], predicted, float(sample["actuals"][horizon])))
    return scores


def _baseline_scores(
    samples: list[dict[str, Any]],
    horizon: str,
    baseline_name: str,
) -> list[dict[str, float]]:
    scores: list[dict[str, float]] = []
    for sample in samples:
        predicted = float(sample["benchmarks"][baseline_name][horizon]["price_usd"])
        scores.append(_score(sample["current_price"], predicted, float(sample["actuals"][horizon])))
    return scores


def _names_from(samples: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Return ensembled model names and benchmark baseline names separately."""
    model_names: set[str] = set()
    baseline_names: set[str] = set()
    for sample in samples:
        model_names.update(sample["models"])
        baseline_names.update(sample["benchmarks"])
    return sorted(model_names), sorted(baseline_names)


def _enriched(model_metrics: dict[str, dict[str, float | int | None]]) -> dict[str, Any]:
    persistence = model_metrics.get("persistence")
    ridge = model_metrics.get("ridge_features")
    enriched: dict[str, Any] = {}
    for name, metrics in model_metrics.items():
        row: dict[str, Any] = dict(metrics)
        mae = metrics.get("mae_pct")
        direction = metrics.get("direction_accuracy")
        if persistence is not None:
            row["mae_delta_vs_persistence_pct"] = (
                round(float(mae) - float(persistence["mae_pct"]), 6)
                if mae is not None and persistence["mae_pct"] is not None
                else None
            )
            row["direction_delta_vs_persistence"] = (
                round(float(direction) - float(persistence["direction_accuracy"]), 6)
                if direction is not None and persistence["direction_accuracy"] is not None
                else None
            )
        if ridge is not None:
            row["mae_delta_vs_ridge_pct"] = (
                round(float(mae) - float(ridge["mae_pct"]), 6)
                if mae is not None and ridge["mae_pct"] is not None
                else None
            )
        enriched[name] = row
    return enriched


def _horizon_report(
    samples: list[dict[str, Any]],
    horizon: str,
    model_names: list[str],
    baseline_names: list[str],
    candidate_names: list[str],
) -> dict[str, Any]:
    model_metrics: dict[str, dict[str, float | int | None]] = {}
    for name in model_names:
        model_metrics[name] = _aggregate(_scores_for_model(samples, horizon, name))
    for name in baseline_names:
        model_metrics[name] = _aggregate(_baseline_scores(samples, horizon, name))
    models = _enriched(model_metrics)

    candidate_maes = {
        name: float(models[name]["mae_pct"])
        for name in candidate_names
        if models[name]["mae_pct"] is not None
    }
    candidate_directions = {
        name: float(models[name]["direction_accuracy"])
        for name in candidate_names
        if models[name]["direction_accuracy"] is not None
    }
    persistence_mae = float(models["persistence"]["mae_pct"]) if "persistence" in models else None
    ridge_mae = float(models["ridge_features"]["mae_pct"]) if "ridge_features" in models else None
    best_candidate_mae = None
    if candidate_maes:
        best_candidate_mae = min(candidate_maes, key=lambda name: candidate_maes[name])
    best_candidate_direction = None
    if candidate_directions:
        best_candidate_direction = max(
            candidate_directions, key=lambda name: candidate_directions[name]
        )
    return {
        "models": models,
        "best_candidate_mae": best_candidate_mae,
        "best_candidate_direction": best_candidate_direction,
        "candidate_beats_persistence": [
            name
            for name, mae in candidate_maes.items()
            if persistence_mae is not None and mae < persistence_mae
        ],
        "candidate_beats_ridge": [
            name
            for name, mae in candidate_maes.items()
            if ridge_mae is not None and mae < ridge_mae
        ],
    }


def _build_by_horizon(
    samples: list[dict[str, Any]],
    model_names: list[str],
    baseline_names: list[str],
    candidate_names: list[str],
) -> dict[str, Any]:
    return {
        f"{hour}h": _horizon_report(
            samples,
            f"{hour}h",
            model_names,
            baseline_names,
            candidate_names,
        )
        for hour in TARGET_HOURS
    }


def _build_by_regime(
    samples: list[dict[str, Any]],
    model_names: list[str],
    baseline_names: list[str],
) -> dict[str, Any]:
    regimes: set[str] = {str(sample["regime"]) for sample in samples}
    result: dict[str, Any] = {}
    for regime in sorted(regimes):
        subset = [sample for sample in samples if str(sample["regime"]) == regime]
        by_horizon: dict[str, Any] = {}
        for hour in TARGET_HOURS:
            horizon = f"{hour}h"
            metrics: dict[str, Any] = {}
            for name in model_names:
                metrics[name] = _aggregate(_scores_for_model(subset, horizon, name))
            for name in baseline_names:
                metrics[name] = _aggregate(_baseline_scores(subset, horizon, name))
            by_horizon[horizon] = metrics
        result[regime] = {"samples": len(subset), "by_horizon": by_horizon}
    return result


def _residual_correlation(
    samples: list[dict[str, Any]],
    model_names: list[str],
    horizon: str,
    *,
    min_samples: int = 5,
) -> dict[str, Any]:
    residuals: dict[str, dict[str, float]] = {name: {} for name in model_names}
    for sample in samples:
        actual = float(sample["actuals"][horizon])
        origin = str(sample["origin_timestamp"])
        for name in model_names:
            if name in sample["models"]:
                predicted = float(sample["models"][name][horizon]["price_usd"])
            else:
                predicted = float(sample["benchmarks"][name][horizon]["price_usd"])
            residuals[name][origin] = (predicted - actual) / actual

    correlation: dict[str, dict[str, float | None]] = {name: {} for name in model_names}
    pair_samples: dict[str, dict[str, int]] = {name: {} for name in model_names}
    for left in model_names:
        for right in model_names:
            common = sorted(set(residuals[left]) & set(residuals[right]))
            pair_samples[left][right] = len(common)
            if left == right:
                correlation[left][right] = 1.0 if common else None
                continue
            if len(common) < min_samples:
                correlation[left][right] = None
                continue
            x = np.asarray([residuals[left][origin] for origin in common], dtype=float)
            y = np.asarray([residuals[right][origin] for origin in common], dtype=float)
            if float(np.std(x)) <= 1e-12 or float(np.std(y)) <= 1e-12:
                correlation[left][right] = None
            else:
                correlation[left][right] = round(float(np.corrcoef(x, y)[0, 1]), 6)
    return {"correlation": correlation, "pair_samples": pair_samples}


def _build_correlation(
    samples: list[dict[str, Any]],
    model_names: list[str],
) -> dict[str, Any]:
    return {
        f"{hour}h": _residual_correlation(samples, model_names, f"{hour}h") for hour in TARGET_HOURS
    }


def _build_runtime(samples: list[dict[str, Any]]) -> dict[str, Any]:
    accumulated: dict[str, list[float]] = {}
    for sample in samples:
        for name, seconds in sample["runtimes"].items():
            accumulated.setdefault(name, []).append(float(seconds))
    return {
        name: {
            "origins": len(values),
            "mean_seconds_per_forecast": round(float(np.mean(values)), 6),
            "total_seconds": round(float(np.sum(values)), 6),
        }
        for name, values in sorted(accumulated.items())
    }


def build_report(
    samples: list[dict[str, Any]],
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the standalone evaluation report for the walk-forward origins."""
    ordered = sorted(samples, key=lambda sample: int(sample["origin_timestamp"]))
    model_names, baseline_names = _names_from(ordered)
    candidate_names = [name for name in CANDIDATE_MODEL_NAMES if name in model_names]
    by_horizon = _build_by_horizon(ordered, model_names, baseline_names, candidate_names)
    correlation_names = sorted(set(model_names) | set(baseline_names))
    return {
        "generated_at": (generated_at or datetime.now(timezone.utc)).isoformat(),
        "origins": len(ordered),
        "model_families": list(CANDIDATE_MODEL_NAMES),
        "comparison_models": list(COMPARISON_MODELS),
        "baselines": baseline_names,
        "production_note": PRODUCTION_NOTE,
        "by_horizon": by_horizon,
        "by_regime": _build_by_regime(ordered, model_names, baseline_names),
        "residual_correlation": _build_correlation(ordered, correlation_names),
        "runtime_seconds": _build_runtime(ordered),
        "interpretation": (
            "Negative MAE deltas mean the candidate beat the referenced model; "
            "correlations are computed on aligned residual errors across origins."
        ),
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _model_table(report: dict[str, Any], horizon: str) -> list[str]:
    models = report["by_horizon"][horizon]["models"]
    lines = [
        "",
        f"### {horizon}",
        "",
        "| Model | Samples | MAE% | Dir% | Δvs persist | Δvs ridge |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, metrics in sorted(models.items()):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(name),
                    str(metrics["samples"]),
                    _fmt(metrics["mae_pct"]),
                    _fmt(metrics["direction_accuracy"]),
                    _fmt(metrics.get("mae_delta_vs_persistence_pct")),
                    _fmt(metrics.get("mae_delta_vs_ridge_pct")),
                ]
            )
            + " |"
        )
    horizon_summary = report["by_horizon"][horizon]
    lines.append("")
    lines.append(
        f"Best candidate by MAE: **{horizon_summary['best_candidate_mae'] or 'none'}**; "
        f"by direction: **{horizon_summary['best_candidate_direction'] or 'none'}**."
    )
    lines.append(
        "Candidates beating persistence MAE: "
        + (", ".join(horizon_summary["candidate_beats_persistence"]) or "none")
        + "; beating ridge_features MAE: "
        + (", ".join(horizon_summary["candidate_beats_ridge"]) or "none")
        + "."
    )
    return lines


def render_markdown(report: dict[str, Any]) -> str:
    """Render the evaluation report as concise markdown."""
    lines = [
        "# Independent model family evaluation",
        "",
        f"Generated: `{report['generated_at']}`",
        f"Walk-forward origins: **{report['origins']}**",
        f"Model families: {', '.join(report['model_families'])}",
        f"Comparison models: {', '.join(report['comparison_models'])}",
        f"Baselines: {', '.join(report['baselines'])}",
        "",
        "Negative MAE deltas mean the candidate beat the referenced model. All models "
        "share identical forecast origins and horizons.",
        "",
    ]
    for horizon in report["by_horizon"]:
        lines.extend(_model_table(report, horizon))

    lines.extend(
        [
            "",
            "## Residual-error correlation",
            "",
            "Correlation is computed on aligned (origin, horizon) residual errors. "
            "High correlation means the candidate is largely redundant with the "
            "existing model; low correlation is the diversity signal being sought.",
            "",
        ]
    )
    for horizon, block in report["residual_correlation"].items():
        correlation = block["correlation"]
        names = sorted(correlation)
        header = "| Model |" + " | ".join(names) + " |"
        divider = "| --- |" + " | ".join(["---:"] * len(names)) + " |"
        lines.extend(["", f"### {horizon}", "", header, divider])
        for left in names:
            lines.append(
                "| "
                + left
                + " | "
                + " | ".join(_fmt(correlation[left].get(right)) for right in names)
                + " |"
            )

    lines.extend(
        [
            "",
            "## Runtime / cost",
            "",
            "| Model | Origins | Mean seconds per forecast | Total seconds |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for name, metrics in sorted(report["runtime_seconds"].items()):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(name),
                    str(metrics["origins"]),
                    _fmt(metrics["mean_seconds_per_forecast"]),
                    _fmt(metrics["total_seconds"]),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Regime breakdown",
            "",
        ]
    )
    for regime, block in sorted(report["by_regime"].items()):
        lines.append(f"Samples by regime `{regime}`: **{block['samples']}**")
        for horizon, metrics in block["by_horizon"].items():
            rows = [
                f"{name} MAE {_fmt(metrics[name]['mae_pct'])} "
                f"(dir {_fmt(metrics[name]['direction_accuracy'])})"
                for name in sorted(metrics)
            ]
            lines.append(f"- {horizon}: " + "; ".join(rows))
        lines.append("")

    lines.extend(["---", "", report["production_note"]])
    return "\n".join(lines).rstrip() + "\n"


def write_report(
    report: dict[str, Any],
    json_path: Path,
    markdown_path: Path,
) -> None:
    """Write the JSON and markdown summaries for the evaluation report."""
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")


def fetch_binance_history(days: int) -> MarketData:
    """Fetch BTCUSDT 1h candles for the research walk-forward (mirrors backtest.py)."""
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    rows: list[list[Any]] = []
    cursor = start_ms
    while cursor < end_ms:
        params: dict[str, str | int] = {
            "symbol": "BTCUSDT",
            "interval": "1h",
            "startTime": cursor,
            "endTime": end_ms,
            "limit": 1000,
        }
        response = requests.get(BINANCE_KLINES, params=params, timeout=30)
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
        opens=np.asarray([float(row[1]) for row in rows], dtype=float),
        highs=np.asarray([float(row[2]) for row in rows], dtype=float),
        lows=np.asarray([float(row[3]) for row in rows], dtype=float),
        closes=np.asarray([float(row[4]) for row in rows], dtype=float),
        volumes=np.asarray([float(row[5]) for row in rows], dtype=float),
    )


def build_and_save_report(
    data: MarketData,
    *,
    first_origin: int,
    last_origin: int,
    num_origins: int,
    json_path: Path,
    markdown_path: Path,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Run the walk-forward evaluation and persist both summaries."""
    samples = run_walk_forward_evaluation(
        data,
        first_origin=first_origin,
        last_origin=last_origin,
        num_origins=num_origins,
    )
    report = build_report(samples, generated_at=generated_at)
    write_report(report, json_path, markdown_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Walk-forward evaluation of independent model-family candidates"
    )
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--origins", type=int, default=40)
    parser.add_argument("--first-origin", type=int, default=513)
    parser.add_argument("--json", type=Path, default=Path("independent_models_report.json"))
    parser.add_argument("--markdown", type=Path, default=Path("independent_models_report.md"))
    args = parser.parse_args()

    data = fetch_binance_history(args.days)
    last_origin = len(data.closes) - max(TARGET_HOURS) - 1
    report = build_and_save_report(
        data,
        first_origin=args.first_origin,
        last_origin=last_origin,
        num_origins=args.origins,
        json_path=args.json,
        markdown_path=args.markdown,
    )
    print(f"Evaluated {report['origins']} walk-forward origins")
    for horizon, block in report["by_horizon"].items():
        print(
            f"{horizon}: best candidate {block['best_candidate_mae']} "
            f"(beats persistence: {', '.join(block['candidate_beats_persistence']) or 'none'}; "
            f"beats ridge: {', '.join(block['candidate_beats_ridge']) or 'none'})"
        )
    print(f"\nSaved {args.json} and {args.markdown}")
    print(PRODUCTION_NOTE)


if __name__ == "__main__":
    main()
