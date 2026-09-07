#!/usr/bin/env python3
"""Evaluate directional forecast signals from durable forecast history.

The report intentionally consumes only matured durable-history rows.  Every row
is scored from ``predicted_change_pct`` and ``actual_change_pct`` already tied to
one forecast origin/target, so segment summaries do not use information that was
unavailable at forecast time.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from btc_timesfm.history.history_store import DEFAULT_DB_PATH, ENSEMBLE_MODEL, ForecastHistoryStore

DEFAULT_HORIZONS = (2, 4, 8, 16)
DEFAULT_LOW_SAMPLE_THRESHOLD = 20
DEFAULT_NEUTRAL_THRESHOLD_PCT = 0.25
LABELS = ("down", "neutral", "up")


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None else None


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return sum(items) / len(items) if items else None


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


def classify_direction(change_pct: float, neutral_threshold_pct: float) -> str:
    """Classify a percentage move as up/down/neutral using a symmetric threshold."""
    if neutral_threshold_pct < 0:
        raise ValueError("neutral_threshold_pct must be >= 0")
    if change_pct > neutral_threshold_pct:
        return "up"
    if change_pct < -neutral_threshold_pct:
        return "down"
    return "neutral"


def _bucket(value: float | None, cuts: tuple[float, float], labels: tuple[str, str, str]) -> str:
    if value is None:
        return "unknown"
    if value < cuts[0]:
        return labels[0]
    if value < cuts[1]:
        return labels[1]
    return labels[2]


def _parse_signal(row: dict[str, Any], neutral_threshold_pct: float) -> dict[str, Any] | None:
    if row.get("actual_target_price_usd") is None:
        return None
    predicted_change = _safe_float(row.get("predicted_change_pct"))
    actual_change = _safe_float(row.get("actual_change_pct"))
    horizon = _safe_float(row.get("horizon_hours"))
    if predicted_change is None or actual_change is None or horizon is None:
        return None
    features = _json_object(row.get("market_features_json"))
    predicted_direction = classify_direction(predicted_change, neutral_threshold_pct)
    actual_direction = classify_direction(actual_change, neutral_threshold_pct)
    magnitude_error = abs(predicted_change - actual_change)
    return {
        "origin_at": row.get("origin_at"),
        "target_at": row.get("target_at"),
        "model_name": str(row.get("model_name") or "unknown"),
        "horizon": f"{int(horizon)}h",
        "regime": str(row.get("regime") or "unknown"),
        "predicted_change_pct": predicted_change,
        "actual_change_pct": actual_change,
        "predicted_direction": predicted_direction,
        "actual_direction": actual_direction,
        "direction_correct": predicted_direction == actual_direction,
        "meaningful_actual_move": actual_direction != "neutral",
        "magnitude_error_pct_points": magnitude_error,
        "volatility_bucket": _bucket(
            _safe_float(features.get("volatility_24h_pct")),
            (1.0, 2.5),
            ("low", "medium", "high"),
        ),
    }


def _empty_confusion() -> dict[str, dict[str, int]]:
    return {actual: {predicted: 0 for predicted in LABELS} for actual in LABELS}


def _warning(samples: int, low_sample_threshold: int) -> str | None:
    if samples == 0:
        return "no_matured_samples"
    if samples < low_sample_threshold:
        return f"low_sample_count:{samples}<{low_sample_threshold}"
    return None


def _summary(
    signals: list[dict[str, Any]],
    *,
    low_sample_threshold: int,
    neutral_threshold_pct: float,
) -> dict[str, Any]:
    samples = len(signals)
    confusion = _empty_confusion()
    for signal in signals:
        confusion[signal["actual_direction"]][signal["predicted_direction"]] += 1

    class_metrics: dict[str, dict[str, Any]] = {}
    recalls: list[float] = []
    for label in LABELS:
        true_positive = confusion[label][label]
        actual_support = sum(confusion[label].values())
        predicted_support = sum(confusion[actual][label] for actual in LABELS)
        precision = true_positive / predicted_support if predicted_support else None
        recall = true_positive / actual_support if actual_support else None
        if recall is not None:
            recalls.append(recall)
        class_metrics[label] = {
            "support": actual_support,
            "predicted": predicted_support,
            "precision": _round(precision),
            "recall": _round(recall),
        }

    meaningful = [signal for signal in signals if signal["meaningful_actual_move"]]
    meaningful_direction_hits = [
        float(signal["direction_correct"])
        for signal in meaningful
        if signal["predicted_direction"] != "neutral"
    ]
    warning = _warning(samples, low_sample_threshold)
    return {
        "samples": samples,
        "neutral_threshold_pct": neutral_threshold_pct,
        "meaningful_samples": len(meaningful),
        "direction_accuracy": _round(_mean(float(s["direction_correct"]) for s in signals)),
        "balanced_direction_accuracy": _round(_mean(recalls)),
        "meaningful_move_accuracy_excluding_neutral_predictions": _round(
            _mean(meaningful_direction_hits)
        ),
        "mean_magnitude_error_pct_points": _round(
            _mean(s["magnitude_error_pct_points"] for s in signals)
        ),
        "confusion_matrix": confusion,
        "precision_recall": class_metrics,
        "reason": warning or "ok",
        "unstable_or_low_sample": bool(warning),
    }


def _segment_report(
    signals: list[dict[str, Any]],
    dimension: str,
    *,
    low_sample_threshold: int,
    neutral_threshold_pct: float,
) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for signal in signals:
        buckets[str(signal.get(dimension) or "unknown")].append(signal)
    return {
        name: _summary(
            bucket,
            low_sample_threshold=low_sample_threshold,
            neutral_threshold_pct=neutral_threshold_pct,
        )
        for name, bucket in sorted(buckets.items())
    }


def build_report(
    rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    model_name: str = ENSEMBLE_MODEL,
    neutral_threshold_pct: float = DEFAULT_NEUTRAL_THRESHOLD_PCT,
    low_sample_threshold: int = DEFAULT_LOW_SAMPLE_THRESHOLD,
) -> dict[str, Any]:
    """Build a JSON-serializable directional signal evaluation report."""
    if neutral_threshold_pct < 0:
        raise ValueError("neutral_threshold_pct must be >= 0")
    if low_sample_threshold < 1:
        raise ValueError("low_sample_threshold must be >= 1")

    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    matured_rows = [row for row in rows if row.get("actual_target_price_usd") is not None]
    signals = [
        signal
        for row in matured_rows
        if str(row.get("model_name") or "unknown") == model_name
        for signal in [_parse_signal(row, neutral_threshold_pct)]
        if signal is not None
    ]
    horizons = sorted(
        {f"{horizon}h" for horizon in DEFAULT_HORIZONS} | {signal["horizon"] for signal in signals},
        key=lambda label: int(label.rstrip("h")),
    )
    by_dimension = {
        dimension: _segment_report(
            signals,
            dimension,
            low_sample_threshold=low_sample_threshold,
            neutral_threshold_pct=neutral_threshold_pct,
        )
        for dimension in ("horizon", "regime", "volatility_bucket")
    }
    for horizon in horizons:
        by_dimension["horizon"].setdefault(
            horizon,
            _summary(
                [],
                low_sample_threshold=low_sample_threshold,
                neutral_threshold_pct=neutral_threshold_pct,
            ),
        )
    return {
        "generated_at": current_time.isoformat(),
        "model_name": model_name,
        "horizons": horizons,
        "neutral_threshold_pct": neutral_threshold_pct,
        "low_sample_threshold": low_sample_threshold,
        "matured_rows": len(matured_rows),
        "evaluated_samples": len(signals),
        "overall": _summary(
            signals,
            low_sample_threshold=low_sample_threshold,
            neutral_threshold_pct=neutral_threshold_pct,
        ),
        "by_dimension": by_dimension,
        "leakage_guard": {
            "source": "durable_forecast_history",
            "uses_only_matured_outcomes": True,
            "forecast_time_inputs": ["predicted_change_pct", "origin_at", "horizon_hours", "regime"],
            "outcome_inputs": ["actual_change_pct", "actual_target_price_usd", "target_at"],
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
        "# Directional-signal evaluation",
        "",
        f"Generated: `{report['generated_at']}`",
        f"Model: **{report['model_name']}**",
        f"Neutral/meaningful-move threshold: **±{report['neutral_threshold_pct']}%**",
        f"Evaluated matured samples: **{report['evaluated_samples']}**",
        "",
        "Direction hits are evaluated separately from magnitude error. Low-sample segments "
        "are marked unstable/inconclusive.",
        "",
        "## Overall",
        "",
        "| Samples | Meaningful | Accuracy | Balanced accuracy | Meaningful accuracy | Mean magnitude error | Reason |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    overall = report["overall"]
    lines.append(
        "| "
        + " | ".join(
            [
                str(overall["samples"]),
                str(overall["meaningful_samples"]),
                _fmt(overall["direction_accuracy"]),
                _fmt(overall["balanced_direction_accuracy"]),
                _fmt(overall["meaningful_move_accuracy_excluding_neutral_predictions"]),
                _fmt(overall["mean_magnitude_error_pct_points"]),
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
                "| Segment | Samples | Meaningful | Accuracy | Balanced accuracy | Unstable | Reason |",
                "| --- | ---: | ---: | ---: | ---: | --- | --- |",
            ]
        )
        for segment, metrics in segments.items():
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(segment),
                        str(metrics["samples"]),
                        str(metrics["meaningful_samples"]),
                        _fmt(metrics["direction_accuracy"]),
                        _fmt(metrics["balanced_direction_accuracy"]),
                        str(metrics["unstable_or_low_sample"]),
                        metrics["reason"],
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
    model_name: str = ENSEMBLE_MODEL,
    neutral_threshold_pct: float = DEFAULT_NEUTRAL_THRESHOLD_PCT,
    low_sample_threshold: int = DEFAULT_LOW_SAMPLE_THRESHOLD,
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
        model_name=model_name,
        neutral_threshold_pct=neutral_threshold_pct,
        low_sample_threshold=low_sample_threshold,
    )
    report["database_verification"] = {**verification, "ok": True}
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate directional-signal evaluation artifacts from durable history"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--json", type=Path, default=Path("directional_signal_evaluation.json"))
    parser.add_argument("--markdown", type=Path, default=Path("directional_signal_evaluation.md"))
    parser.add_argument("--model-name", default=ENSEMBLE_MODEL)
    parser.add_argument("--neutral-threshold-pct", type=float, default=DEFAULT_NEUTRAL_THRESHOLD_PCT)
    parser.add_argument("--low-sample-threshold", type=int, default=DEFAULT_LOW_SAMPLE_THRESHOLD)
    args = parser.parse_args()
    report = generate_report(
        args.db,
        json_path=args.json,
        markdown_path=args.markdown,
        model_name=args.model_name,
        neutral_threshold_pct=args.neutral_threshold_pct,
        low_sample_threshold=args.low_sample_threshold,
    )
    print(
        json.dumps(
            {
                "generated_at": report["generated_at"],
                "evaluated_samples": report["evaluated_samples"],
                "json": str(args.json),
                "markdown": str(args.markdown),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
