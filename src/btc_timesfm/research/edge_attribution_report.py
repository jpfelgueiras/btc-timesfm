#!/usr/bin/env python3
"""Generate ensemble-vs-persistence edge attribution reports from durable history."""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from btc_timesfm.forecasting.statistical_significance import paired_bootstrap_comparison
from btc_timesfm.history.history_store import DEFAULT_DB_PATH, ENSEMBLE_MODEL, ForecastHistoryStore

DEFAULT_HORIZONS = (2, 4, 8, 16)
DEFAULT_LOW_SAMPLE_THRESHOLD = 20
DEFAULT_MIN_PAIRED_SAMPLES = 32
DEFAULT_BOOTSTRAP_ITERATIONS = 1000
PERSISTENCE_MODEL = "persistence"


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _safe_int(value: Any) -> int | None:
    number = _safe_float(value)
    return int(number) if number is not None else None


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return sum(items) / len(items) if items else None


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None else None


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _bucket(value: float | None, cuts: tuple[float, float], labels: tuple[str, str, str]) -> str:
    if value is None:
        return "unknown"
    if value < cuts[0]:
        return labels[0]
    if value < cuts[1]:
        return labels[1]
    return labels[2]


def _time_of_day(hour: int | None) -> str:
    if hour is None:
        return "unknown"
    if 0 <= hour < 6:
        return "00-06"
    if 6 <= hour < 12:
        return "06-12"
    if 12 <= hour < 18:
        return "12-18"
    return "18-24"


def _weekday(value: int | None) -> str:
    names = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    return names[value] if value is not None and 0 <= value < 7 else "unknown"


def _feature_set_version(row: dict[str, Any]) -> str:
    manifest = _json_object(row.get("experiment_manifest_json"))
    configuration = (
        manifest.get("configuration") if isinstance(manifest.get("configuration"), dict) else {}
    )
    version = configuration.get("feature_set_version") or manifest.get("feature_set_version")
    return str(version or row.get("configuration_id") or "unknown")


def _confidence_bucket(row: dict[str, Any]) -> str:
    # model_agreement is emitted by the ensemble; higher agreement is treated as higher confidence.
    return _bucket(
        _safe_float(row.get("model_agreement")),
        (0.4, 0.7),
        ("low", "medium", "high"),
    )


def _model_contribution(rows: list[dict[str, Any]]) -> str:
    candidates: list[tuple[float, str]] = []
    for row in rows:
        name = str(row.get("model_name") or "")
        if name in {"", ENSEMBLE_MODEL, PERSISTENCE_MODEL}:
            continue
        weight = _safe_float(row.get("ensemble_weight"))
        if weight is not None:
            candidates.append((weight, name))
    if not candidates:
        return "unknown"
    weight, name = max(candidates, key=lambda item: (item[0], item[1]))
    return f"top:{name}" if weight >= 0.34 else "diversified"


def _segment_values(pair: dict[str, Any]) -> dict[str, str]:
    row = pair["ensemble_row"]
    features = _json_object(row.get("market_features_json"))
    origin = _parse_timestamp(row.get("origin_at"))
    hour = _safe_int(features.get("hour_utc"))
    weekday = _safe_int(features.get("weekday_utc"))
    if hour is None and origin is not None:
        hour = origin.hour
    if weekday is None and origin is not None:
        weekday = origin.weekday()

    momentum = _safe_float(features.get("momentum_24h_pct"))

    return {
        "horizon": f"{int(row['horizon_hours'])}h",
        "regime": str(row.get("regime") or "unknown"),
        "volatility_bucket": _bucket(
            _safe_float(features.get("volatility_24h_pct")),
            (1.0, 2.5),
            ("low", "medium", "high"),
        ),
        "trend_strength": _bucket(
            abs(momentum) if momentum is not None else None,
            (1.0, 3.0),
            ("low", "medium", "high"),
        ),
        "time_of_day": _time_of_day(hour),
        "day_of_week": _weekday(weekday),
        "confidence_bucket": _confidence_bucket(row),
        "feature_set_version": _feature_set_version(row),
        "model_contribution": pair["model_contribution"],
    }


def _build_pairs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, Any, Any], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("actual_target_price_usd") is None:
            continue
        grouped[(row.get("origin_at"), row.get("horizon_hours"), row.get("target_at"))].append(row)

    pairs: list[dict[str, Any]] = []
    for key_rows in grouped.values():
        by_model = {str(row.get("model_name")): row for row in key_rows}
        ensemble = by_model.get(ENSEMBLE_MODEL)
        persistence = by_model.get(PERSISTENCE_MODEL)
        if not ensemble or not persistence:
            continue
        ensemble_error = _safe_float(ensemble.get("absolute_error_pct"))
        persistence_error = _safe_float(persistence.get("absolute_error_pct"))
        if ensemble_error is None or persistence_error is None:
            continue
        pair = {
            "ensemble_row": ensemble,
            "persistence_row": persistence,
            "ensemble_abs_error_pct": ensemble_error,
            "persistence_abs_error_pct": persistence_error,
            "ensemble_direction": _safe_float(ensemble.get("direction_correct")),
            "persistence_direction": _safe_float(persistence.get("direction_correct")),
            "ensemble_bias_pct": _safe_float(ensemble.get("signed_error_pct")),
            "persistence_bias_pct": _safe_float(persistence.get("signed_error_pct")),
            "origin_at": ensemble.get("origin_at"),
            "model_contribution": _model_contribution(key_rows),
        }
        pair["segments"] = _segment_values(pair)
        pairs.append(pair)
    return sorted(pairs, key=lambda pair: (str(pair.get("origin_at")), pair["segments"]["horizon"]))


def _warning(samples: int, low_sample_threshold: int) -> str | None:
    if samples == 0:
        return "no_paired_samples"
    if samples < low_sample_threshold:
        return f"low_sample_count:{samples}<{low_sample_threshold}"
    return None


def _paired_summary(
    pairs: list[dict[str, Any]],
    *,
    low_sample_threshold: int,
    min_paired_samples: int,
    bootstrap_iterations: int,
) -> dict[str, Any]:
    samples = len(pairs)
    ensemble_errors = [pair["ensemble_abs_error_pct"] for pair in pairs]
    persistence_errors = [pair["persistence_abs_error_pct"] for pair in pairs]
    mae_delta = [base - candidate for candidate, base in zip(ensemble_errors, persistence_errors)]
    direction_pairs = [
        (pair["ensemble_direction"], pair["persistence_direction"])
        for pair in pairs
        if pair["ensemble_direction"] is not None and pair["persistence_direction"] is not None
    ]
    bias_delta = [
        abs(pair["persistence_bias_pct"]) - abs(pair["ensemble_bias_pct"])
        for pair in pairs
        if pair["ensemble_bias_pct"] is not None and pair["persistence_bias_pct"] is not None
    ]
    significance = paired_bootstrap_comparison(
        ensemble_errors,
        persistence_errors,
        metric="mae_pct",
        lower_is_better=True,
        iterations=bootstrap_iterations,
        min_samples=min_paired_samples,
    )
    warning = _warning(samples, low_sample_threshold)
    unstable = bool(warning) or significance.get("conclusion") == "inconclusive"
    return {
        "samples": samples,
        "ensemble_mae_pct": _round(_mean(ensemble_errors)),
        "persistence_mae_pct": _round(_mean(persistence_errors)),
        "mae_delta_pct_points": _round(_mean(mae_delta)),
        "relative_mae_improvement": _round(significance.get("relative_effect_size")),
        "direction_accuracy_delta": _round(
            _mean(candidate - baseline for candidate, baseline in direction_pairs)
            if direction_pairs
            else None
        ),
        "absolute_bias_delta_pct_points": _round(_mean(bias_delta)),
        "confidence_interval": significance["improvement_ci"],
        "probability_ensemble_better": significance["probability_candidate_better"],
        "conclusion": significance["conclusion"],
        "reason": warning or significance["reason"],
        "unstable_or_low_sample": unstable,
        "bootstrap": significance,
    }


def _segment_report(
    pairs: list[dict[str, Any]],
    dimension: str,
    *,
    low_sample_threshold: int,
    min_paired_samples: int,
    bootstrap_iterations: int,
) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pair in pairs:
        buckets[pair["segments"].get(dimension, "unknown")].append(pair)
    return {
        name: _paired_summary(
            bucket_pairs,
            low_sample_threshold=low_sample_threshold,
            min_paired_samples=min_paired_samples,
            bootstrap_iterations=bootstrap_iterations,
        )
        for name, bucket_pairs in sorted(buckets.items())
    }


def _monthly_stability(
    pairs: list[dict[str, Any]],
    *,
    low_sample_threshold: int,
    min_paired_samples: int,
    bootstrap_iterations: int,
) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for pair in pairs:
        parsed = _parse_timestamp(pair.get("origin_at"))
        buckets[parsed.strftime("%Y-%m") if parsed else "unknown"].append(pair)
    periods = {
        name: _paired_summary(
            bucket_pairs,
            low_sample_threshold=low_sample_threshold,
            min_paired_samples=min_paired_samples,
            bootstrap_iterations=bootstrap_iterations,
        )
        for name, bucket_pairs in sorted(buckets.items())
    }
    deltas = [
        value["mae_delta_pct_points"]
        for value in periods.values()
        if value["mae_delta_pct_points"] is not None
    ]
    return {
        "period": "calendar_month",
        "periods": periods,
        "stable_positive_periods": sum(1 for value in deltas if value > 0),
        "stable_negative_periods": sum(1 for value in deltas if value < 0),
        "unstable_periods": sum(1 for value in periods.values() if value["unstable_or_low_sample"]),
    }


def build_report(
    rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    low_sample_threshold: int = DEFAULT_LOW_SAMPLE_THRESHOLD,
    min_paired_samples: int = DEFAULT_MIN_PAIRED_SAMPLES,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
) -> dict[str, Any]:
    """Build a reproducible JSON-serializable edge attribution report."""
    if low_sample_threshold < 1:
        raise ValueError("low_sample_threshold must be >= 1")
    if min_paired_samples < 1:
        raise ValueError("min_paired_samples must be >= 1")
    if bootstrap_iterations < 100:
        raise ValueError("bootstrap_iterations must be >= 100")

    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    matured_rows = [row for row in rows if row.get("actual_target_price_usd") is not None]
    pairs = _build_pairs(matured_rows)
    horizons = sorted(
        {f"{horizon}h" for horizon in DEFAULT_HORIZONS}
        | {pair["segments"]["horizon"] for pair in pairs},
        key=lambda label: int(label.rstrip("h")),
    )
    dimensions = (
        "horizon",
        "regime",
        "volatility_bucket",
        "trend_strength",
        "time_of_day",
        "day_of_week",
        "confidence_bucket",
        "feature_set_version",
        "model_contribution",
    )
    by_dimension = {
        dimension: _segment_report(
            pairs,
            dimension,
            low_sample_threshold=low_sample_threshold,
            min_paired_samples=min_paired_samples,
            bootstrap_iterations=bootstrap_iterations,
        )
        for dimension in dimensions
    }
    for horizon in horizons:
        by_dimension["horizon"].setdefault(
            horizon,
            _paired_summary(
                [],
                low_sample_threshold=low_sample_threshold,
                min_paired_samples=min_paired_samples,
                bootstrap_iterations=bootstrap_iterations,
            ),
        )

    return {
        "generated_at": current_time.isoformat(),
        "ensemble_model": ENSEMBLE_MODEL,
        "baseline_model": PERSISTENCE_MODEL,
        "matured_rows": len(matured_rows),
        "paired_samples": len(pairs),
        "low_sample_threshold": low_sample_threshold,
        "min_paired_samples": min_paired_samples,
        "horizons": horizons,
        "overall": _paired_summary(
            pairs,
            low_sample_threshold=low_sample_threshold,
            min_paired_samples=min_paired_samples,
            bootstrap_iterations=bootstrap_iterations,
        ),
        "by_dimension": by_dimension,
        "stability_over_time": _monthly_stability(
            pairs,
            low_sample_threshold=low_sample_threshold,
            min_paired_samples=min_paired_samples,
            bootstrap_iterations=bootstrap_iterations,
        ),
        "reproducibility": {
            "source": "durable_forecast_history",
            "manifest_fields": [
                "experiment_run_id",
                "configuration_id",
                "experiment_manifest_json",
            ],
        },
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Edge attribution report",
        "",
        f"Generated: `{report['generated_at']}`",
        f"Paired ensemble-vs-persistence samples: **{report['paired_samples']}**",
        f"Low-sample threshold: **{report['low_sample_threshold']}**",
        "",
        "Positive MAE delta means the ensemble beat persistence. Low-sample or inconclusive "
        "segments are marked as unstable.",
        "",
        "## Overall",
        "",
        "| Samples | Ensemble MAE | Persistence MAE | MAE delta | 95% CI | Direction delta | Conclusion | Reason |",
        "| ---: | ---: | ---: | ---: | --- | ---: | --- | --- |",
    ]
    overall = report["overall"]
    ci = overall["confidence_interval"]
    lines.append(
        "| "
        + " | ".join(
            [
                str(overall["samples"]),
                _fmt(overall["ensemble_mae_pct"]),
                _fmt(overall["persistence_mae_pct"]),
                _fmt(overall["mae_delta_pct_points"]),
                f"[{_fmt(ci['lower'])}, {_fmt(ci['upper'])}]",
                _fmt(overall["direction_accuracy_delta"]),
                overall["conclusion"],
                overall["reason"],
            ]
        )
        + " |"
    )

    for dimension, segments in report["by_dimension"].items():
        lines.extend(
            [
                "",
                f"## By {dimension.replace('_', ' ')}",
                "",
                "| Segment | Samples | Ensemble MAE | Persistence MAE | MAE delta | CI | Direction delta | Unstable | Conclusion |",
                "| --- | ---: | ---: | ---: | ---: | --- | ---: | --- | --- |",
            ]
        )
        for segment, metrics in segments.items():
            ci = metrics["confidence_interval"]
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(segment),
                        str(metrics["samples"]),
                        _fmt(metrics["ensemble_mae_pct"]),
                        _fmt(metrics["persistence_mae_pct"]),
                        _fmt(metrics["mae_delta_pct_points"]),
                        f"[{_fmt(ci['lower'])}, {_fmt(ci['upper'])}]",
                        _fmt(metrics["direction_accuracy_delta"]),
                        str(metrics["unstable_or_low_sample"]),
                        metrics["conclusion"],
                    ]
                )
                + " |"
            )
    return "\n".join(lines).rstrip() + "\n"


def generate_report(
    db_path: Path,
    *,
    json_path: Path,
    markdown_path: Path,
    low_sample_threshold: int = DEFAULT_LOW_SAMPLE_THRESHOLD,
    min_paired_samples: int = DEFAULT_MIN_PAIRED_SAMPLES,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
) -> dict[str, Any]:
    store = ForecastHistoryStore(db_path)
    verification = store.verify()
    verification_ok = (
        verification.get("integrity") == "ok"
        and int(verification.get("foreign_key_violations", 1)) == 0
        and verification.get("schema_version") == verification.get("supported_schema_version")
    )
    if not verification_ok:
        raise RuntimeError(f"forecast history failed verification: {verification}")
    report = build_report(
        store.export_rows(),
        low_sample_threshold=low_sample_threshold,
        min_paired_samples=min_paired_samples,
        bootstrap_iterations=bootstrap_iterations,
    )
    report["database_verification"] = {**verification, "ok": True}
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate edge attribution report artifacts from durable forecast history"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--json", type=Path, default=Path("edge_attribution_report.json"))
    parser.add_argument("--markdown", type=Path, default=Path("edge_attribution_report.md"))
    parser.add_argument("--low-sample-threshold", type=int, default=DEFAULT_LOW_SAMPLE_THRESHOLD)
    parser.add_argument("--min-paired-samples", type=int, default=DEFAULT_MIN_PAIRED_SAMPLES)
    parser.add_argument("--bootstrap-iterations", type=int, default=DEFAULT_BOOTSTRAP_ITERATIONS)
    args = parser.parse_args()
    report = generate_report(
        args.db,
        json_path=args.json,
        markdown_path=args.markdown,
        low_sample_threshold=args.low_sample_threshold,
        min_paired_samples=args.min_paired_samples,
        bootstrap_iterations=args.bootstrap_iterations,
    )
    print(
        json.dumps(
            {
                "generated_at": report["generated_at"],
                "paired_samples": report["paired_samples"],
                "json": str(args.json),
                "markdown": str(args.markdown),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
