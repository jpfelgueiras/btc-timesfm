#!/usr/bin/env python3
"""Evaluate BTC forecast reliability around scheduled macro-release events.

Each matured durable-history row is segmented by how close its forecast *origin*
is to a scheduled macro event:

* ``pre_event`` -- origin is within ``window_hours`` before an event (the event
  is the nearest event and has not happened yet at forecast time).
* ``post_event`` -- origin is within ``window_hours`` after an event.
* ``non_event`` -- no event is within ``window_hours`` before or after the
  origin.

Segmentation consumes only the announced event timestamp (public information
released ahead of schedule) plus the forecast origin. No event outcome or
content is ever read, so a pre-event forecast cannot be influenced by a future
release. By default only ``known_scheduled`` events are used, so the schedule
was in fact public before each pre-event forecast was made.

All forecasts are evaluated strictly on matured outcomes whose ``target_at``
has passed at the report's ``now``; sample sizes are reported per segment and
any low-sample segment is explicitly flagged.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from btc_timesfm.history.history_store import DEFAULT_DB_PATH, ENSEMBLE_MODEL, ForecastHistoryStore
from btc_timesfm.research.event_calendar import CalendarEvent, EventCalendar, default_calendar

EVALUATION_VERSION = 1
DEFAULT_WINDOW_HOURS = 24
DEFAULT_LOW_SAMPLE_THRESHOLD = 20
DEFAULT_NEUTRAL_THRESHOLD_PCT = 0.25
DEFAULT_BOOTSTRAP_ITERATIONS = 2000
DEFAULT_MIN_COMPARE_SAMPLES = 24
_BOOTSTRAP_BATCH = 1000
_BOOTSTRAP_SEED = 0
_COMPARED_METRICS = (
    ("mae_pct_points", False),
    ("direction_accuracy", True),
    ("q10_q90_coverage", True),
)


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


def classify_direction(change_pct: float, neutral_threshold_pct: float) -> str:
    """Classify a percentage move as up/down/neutral using a symmetric threshold."""
    if neutral_threshold_pct < 0:
        raise ValueError("neutral_threshold_pct must be >= 0")
    if change_pct > neutral_threshold_pct:
        return "up"
    if change_pct < -neutral_threshold_pct:
        return "down"
    return "neutral"


def _warning(samples: int, low_sample_threshold: int) -> str | None:
    if samples == 0:
        return "no_samples"
    if samples < low_sample_threshold:
        return f"low_sample_count:{samples}<{low_sample_threshold}"
    return None


def _row_values(row: dict[str, Any], neutral_threshold_pct: float) -> dict[str, Any]:
    """Extract error, direction, and calibration signals from one matured row."""
    predicted_change = _safe_float(row.get("predicted_change_pct"))
    actual_change = _safe_float(row.get("actual_change_pct"))

    error_pp = _safe_float(row.get("absolute_error_pct"))
    signed_error_pp = _safe_float(row.get("signed_error_pct"))
    if predicted_change is not None and actual_change is not None:
        if error_pp is None:
            error_pp = abs(predicted_change - actual_change)
        if signed_error_pp is None:
            signed_error_pp = predicted_change - actual_change

    direction_correct: bool | None = None
    predicted_direction: str | None = None
    actual_direction: str | None = None
    if predicted_change is not None and actual_change is not None:
        predicted_direction = classify_direction(predicted_change, neutral_threshold_pct)
        actual_direction = classify_direction(actual_change, neutral_threshold_pct)
        direction_correct = predicted_direction == actual_direction

    covered: bool | None = None
    within_q10_q90 = _safe_float(row.get("within_q10_q90"))
    if within_q10_q90 is not None:
        covered = within_q10_q90 > 0.5
    else:
        q10 = _safe_float(row.get("q10_usd"))
        q90 = _safe_float(row.get("q90_usd"))
        actual_price = _safe_float(row.get("actual_target_price_usd"))
        if q10 is not None and q90 is not None and actual_price is not None:
            covered = q10 <= actual_price <= q90

    actual_price = _safe_float(row.get("actual_target_price_usd"))
    q50 = _safe_float(row.get("q50_usd"))
    q50_mae_pct: float | None = None
    q50_bias_pct: float | None = None
    if q50 is not None and actual_price is not None and actual_price != 0:
        q50_mae_pct = abs(q50 - actual_price) / abs(actual_price) * 100.0
        q50_bias_pct = (q50 - actual_price) / abs(actual_price) * 100.0

    return {
        "error_pp": error_pp,
        "signed_error_pp": signed_error_pp,
        "direction_correct": direction_correct,
        "predicted_direction": predicted_direction,
        "actual_direction": actual_direction,
        "covered": covered,
        "q50_mae_pct": q50_mae_pct,
        "q50_bias_pct": q50_bias_pct,
    }


def _segment_metrics(
    rows: list[dict[str, Any]],
    *,
    neutral_threshold_pct: float,
    low_sample_threshold: int,
) -> dict[str, Any]:
    errors = [row["error_pp"] for row in rows if row["error_pp"] is not None]
    signed = [row["signed_error_pp"] for row in rows if row["signed_error_pp"] is not None]
    directions = [
        float(row["direction_correct"]) for row in rows if row["direction_correct"] is not None
    ]
    covered = [float(row["covered"]) for row in rows if row["covered"] is not None]
    q50_mae = [row["q50_mae_pct"] for row in rows if row["q50_mae_pct"] is not None]
    q50_bias = [row["q50_bias_pct"] for row in rows if row["q50_bias_pct"] is not None]
    samples = len(rows)
    warning = _warning(samples, low_sample_threshold)
    return {
        "samples": samples,
        "low_sample_threshold": low_sample_threshold,
        "mae_pct_points": _round(_mean(errors)),
        "mean_signed_error_pct_points": _round(_mean(signed)),
        "direction_accuracy": _round(_mean(directions)),
        "direction_samples": len(directions),
        "q10_q90_coverage": _round(_mean(covered)),
        "coverage_samples": len(covered),
        "q50_mae_pct": _round(_mean(q50_mae)),
        "q50_bias_pct": _round(_mean(q50_bias)),
        "q50_samples": len(q50_mae),
        "unstable_or_low_sample": bool(warning),
        "reason": warning or "ok",
    }


def _two_sample_bootstrap(
    segment_values: list[float],
    baseline_values: list[float],
    *,
    higher_is_better: bool,
    iterations: int,
    min_samples: int,
    seed: int = _BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Deterministic unpaired bootstrap comparing two independent group means."""
    if len(segment_values) < min_samples or len(baseline_values) < min_samples:
        return {
            "samples": {"segment": len(segment_values), "baseline": len(baseline_values)},
            "segment_mean": _round(_mean(segment_values)),
            "baseline_mean": _round(_mean(baseline_values)),
            "mean_difference": None,
            "confidence_interval": {"lower": None, "upper": None},
            "probability_segment_better": None,
            "conclusion": "inconclusive",
            "reason": "insufficient_samples",
        }
    segment = np.asarray(segment_values, dtype=np.float64)
    baseline = np.asarray(baseline_values, dtype=np.float64)
    rng = np.random.default_rng(seed)
    differences: np.ndarray = np.empty(iterations, dtype=np.float64)
    segment_count = segment.shape[0]
    baseline_count = baseline.shape[0]
    for start in range(0, iterations, _BOOTSTRAP_BATCH):
        count = min(_BOOTSTRAP_BATCH, iterations - start)
        segment_boot = rng.integers(0, segment_count, size=(count, segment_count))
        baseline_boot = rng.integers(0, baseline_count, size=(count, baseline_count))
        differences[start : start + count] = np.mean(segment[segment_boot], axis=1) - np.mean(
            baseline[baseline_boot], axis=1
        )
    lower, upper = np.quantile(differences, [0.025, 0.975])
    if higher_is_better:
        probability_segment_better = float(np.mean(differences > 0.0))
        if lower > 0.0:
            conclusion, reason = "segment_better", "confidence_interval_above_zero"
        elif upper < 0.0:
            conclusion, reason = "non_event_better", "confidence_interval_below_zero"
        else:
            conclusion, reason = "inconclusive", "confidence_interval_crosses_zero"
    else:
        probability_segment_better = float(np.mean(differences < 0.0))
        if upper < 0.0:
            conclusion, reason = "segment_better", "confidence_interval_below_zero"
        elif lower > 0.0:
            conclusion, reason = "non_event_better", "confidence_interval_above_zero"
        else:
            conclusion, reason = "inconclusive", "confidence_interval_crosses_zero"
    return {
        "samples": {"segment": len(segment_values), "baseline": len(baseline_values)},
        "segment_mean": round(float(np.mean(segment)), 6),
        "baseline_mean": round(float(np.mean(baseline)), 6),
        "mean_difference": round(float(np.mean(differences)), 6),
        "confidence_interval": {
            "lower": round(float(lower), 6),
            "upper": round(float(upper), 6),
        },
        "probability_segment_better": round(probability_segment_better, 6),
        "conclusion": conclusion,
        "reason": reason,
    }


def _metric_values(rows: list[dict[str, Any]], metric: str) -> list[float]:
    if metric == "mae_pct_points":
        return [float(row["error_pp"]) for row in rows if row["error_pp"] is not None]
    if metric == "direction_accuracy":
        return [
            float(row["direction_correct"]) for row in rows if row["direction_correct"] is not None
        ]
    if metric == "q10_q90_coverage":
        return [float(row["covered"]) for row in rows if row["covered"] is not None]
    raise ValueError(f"unsupported metric: {metric}")


def _segment_rows(
    rows: list[dict[str, Any]],
    calendar: EventCalendar,
    *,
    window_hours: float,
    known_scheduled_only: bool,
) -> tuple[dict[int, str], dict[int, CalendarEvent | None]]:
    """Bucket rows into segments, keyed by row identity.

    Returns ``(segment_by_row, governing_event_by_row)``. A positive offset to
    the nearest event means the event is still in the future at forecast time
    (``pre_event``); a negative offset means it already happened
    (``post_event``). Rows farther than ``window_hours`` from every event are
    ``non_event``.
    """
    window_seconds = window_hours * 3600.0
    segment_by_row: dict[int, str] = {}
    governing_event_by_row: dict[int, CalendarEvent | None] = {}
    active_events = [
        event for event in calendar.events if event.known_scheduled or not known_scheduled_only
    ]
    for row in rows:
        origin = _parse_timestamp(row.get("origin_at"))
        if origin is None:
            continue
        row_key = id(row)
        nearest: tuple[CalendarEvent, timedelta] | None = None
        for event in active_events:
            delta = event.timestamp - origin
            offset = abs(delta.total_seconds())
            if offset > window_seconds:
                continue
            if nearest is None or offset < abs(nearest[1].total_seconds()):
                nearest = (event, delta)
        if nearest is None:
            segment_by_row[row_key] = "non_event"
            governing_event_by_row[row_key] = None
            continue
        event, delta = nearest
        segment = "pre_event" if delta.total_seconds() >= 0.0 else "post_event"
        segment_by_row[row_key] = segment
        governing_event_by_row[row_key] = event
    return segment_by_row, governing_event_by_row


def build_report(
    rows: list[dict[str, Any]],
    calendar: EventCalendar | None = None,
    *,
    now: datetime | None = None,
    model_name: str = ENSEMBLE_MODEL,
    window_hours: float = DEFAULT_WINDOW_HOURS,
    neutral_threshold_pct: float = DEFAULT_NEUTRAL_THRESHOLD_PCT,
    known_scheduled_only: bool = True,
    low_sample_threshold: int = DEFAULT_LOW_SAMPLE_THRESHOLD,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    min_compare_samples: int = DEFAULT_MIN_COMPARE_SAMPLES,
) -> dict[str, Any]:
    """Build a JSON-serializable event-aware forecast reliability report."""
    if window_hours <= 0:
        raise ValueError("window_hours must be positive")
    if neutral_threshold_pct < 0:
        raise ValueError("neutral_threshold_pct must be >= 0")
    if low_sample_threshold < 1:
        raise ValueError("low_sample_threshold must be >= 1")
    if bootstrap_iterations < 100:
        raise ValueError("bootstrap_iterations must be >= 100")
    if min_compare_samples < 1:
        raise ValueError("min_compare_samples must be >= 1")

    event_calendar = calendar if calendar is not None else default_calendar()
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    excluded: dict[str, int] = defaultdict(int)
    evaluated: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row in rows:
        if row.get("actual_target_price_usd") is None:
            excluded["unresolved_outcome"] += 1
            continue
        if str(row.get("model_name") or "unknown") != model_name:
            excluded["unsupported_model"] += 1
            continue
        origin = _parse_timestamp(row.get("origin_at"))
        if origin is None:
            excluded["unparseable_origin_at"] += 1
            continue
        target = _parse_timestamp(row.get("target_at"))
        if target is None or target > current_time:
            excluded["target_not_resolved_by_now"] += 1
            continue
        values = _row_values(row, neutral_threshold_pct)
        if values["error_pp"] is None:
            excluded["missing_error_metrics"] += 1
            continue
        evaluated.append((row, values))

    matured_rows = [row for row, _ in evaluated]
    segment_by_row, governing_event_by_row = _segment_rows(
        matured_rows,
        event_calendar,
        window_hours=window_hours,
        known_scheduled_only=known_scheduled_only,
    )
    segment_values: dict[str, list[dict[str, Any]]] = {
        segment: [values for row, values in evaluated if segment_by_row.get(id(row)) == segment]
        for segment in ("pre_event", "post_event", "non_event")
    }

    segments_report = {
        segment: _segment_metrics(
            segment_values.get(segment, []),
            neutral_threshold_pct=neutral_threshold_pct,
            low_sample_threshold=low_sample_threshold,
        )
        for segment in ("pre_event", "post_event", "non_event")
    }

    by_horizon: dict[str, dict[str, dict[str, Any]]] = {}
    for segment in ("pre_event", "post_event", "non_event"):
        horizon_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row, values in evaluated:
            if segment_by_row.get(id(row)) != segment:
                continue
            horizon_groups[_horizon_label(row.get("horizon_hours"))].append(values)
        by_horizon[segment] = {
            horizon: _segment_metrics(
                group,
                neutral_threshold_pct=neutral_threshold_pct,
                low_sample_threshold=low_sample_threshold,
            )
            for horizon, group in sorted(horizon_groups.items())
        }

    by_category: dict[str, dict[str, Any]] = {}
    for segment in ("pre_event", "post_event", "non_event"):
        counts: dict[str, int] = defaultdict(int)
        sample_count = 0
        for row, _ in evaluated:
            if segment_by_row.get(id(row)) != segment:
                continue
            sample_count += 1
            event = governing_event_by_row.get(id(row))
            if event is not None:
                counts[event.category] += 1
        by_category[segment] = {
            "counts": dict(sorted(counts.items())),
            "sample_count": sample_count,
        }

    comparisons: dict[str, dict[str, dict[str, Any]]] = {}
    non_event_values = segment_values["non_event"]
    for metric, higher_is_better in _COMPARED_METRICS:
        baseline = _metric_values(non_event_values, metric)
        comparisons[metric] = {
            "pre_event_vs_non_event": _two_sample_bootstrap(
                _metric_values(segment_values["pre_event"], metric),
                baseline,
                higher_is_better=higher_is_better,
                iterations=bootstrap_iterations,
                min_samples=min_compare_samples,
            ),
            "post_event_vs_non_event": _two_sample_bootstrap(
                _metric_values(segment_values["post_event"], metric),
                baseline,
                higher_is_better=higher_is_better,
                iterations=bootstrap_iterations,
                min_samples=min_compare_samples,
            ),
        }

    active_event_count = sum(
        1 for event in event_calendar.events if event.known_scheduled or not known_scheduled_only
    )

    return {
        "generated_at": current_time.isoformat(),
        "evaluation_version": EVALUATION_VERSION,
        "model_name": model_name,
        "window_hours": window_hours,
        "segmentation": "nearest_event_by_forecast_origin",
        "known_scheduled_only": known_scheduled_only,
        "low_sample_threshold": low_sample_threshold,
        "neutral_threshold_pct": neutral_threshold_pct,
        "calendar": event_calendar.to_dict(),
        "segmentation_active_events": active_event_count,
        "input_rows": len(rows),
        "matured_rows": len(matured_rows),
        "evaluated_rows": len(evaluated),
        "excluded": dict(excluded),
        "segments": segments_report,
        "sample_size_limits": {
            "note": (
                "segments with fewer samples than the low-sample threshold are flagged "
                "unstable; bootstrap comparisons require both groups to meet the minimum."
            ),
            "low_sample_threshold": low_sample_threshold,
            "min_compare_samples": min_compare_samples,
        },
        "by_horizon": by_horizon,
        "by_category": by_category,
        "comparisons": comparisons,
        "leakage_guard": {
            "segmentation_inputs": ["event_timestamp", "origin_at"],
            "uses_event_outcomes": False,
            "uses_event_content": False,
            "uses_only_matured_outcomes": True,
            "pre_event_rule": (
                "a row is pre_event only if its origin precedes the nearest event; "
                "classification uses the announced schedule timestamp, never a release result."
            ),
            "now": current_time.isoformat(),
        },
        "policy_note": (
            "event proximity may influence confidence or abstention only after the "
            "segmented difference is validated out of sample."
        ),
        "reproducibility": {
            "source": "durable_forecast_history",
            "row_fields": [
                "origin_at",
                "target_at",
                "model_name",
                "horizon_hours",
                "predicted_change_pct",
                "actual_change_pct",
                "absolute_error_pct",
                "signed_error_pct",
                "q10_usd",
                "q50_usd",
                "q90_usd",
                "within_q10_q90",
                "actual_target_price_usd",
            ],
            "event_calendar": "embedded_defaults_or_loaded_json",
        },
    }


def _horizon_label(horizon: Any) -> str:
    try:
        value = int(horizon)
    except (TypeError, ValueError):
        return "unknown"
    return f"{value}h"


def generate_report(
    db_path: Path,
    *,
    json_path: Path,
    calendar: EventCalendar | None = None,
    calendar_path: Path | None = None,
    model_name: str = ENSEMBLE_MODEL,
    window_hours: float = DEFAULT_WINDOW_HOURS,
    known_scheduled_only: bool = True,
    low_sample_threshold: int = DEFAULT_LOW_SAMPLE_THRESHOLD,
    now: datetime | None = None,
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
    event_calendar = (
        EventCalendar.from_json(calendar_path)
        if calendar_path is not None
        else (calendar if calendar is not None else default_calendar())
    )
    report = build_report(
        store.export_rows(),
        event_calendar,
        model_name=model_name,
        window_hours=window_hours,
        known_scheduled_only=known_scheduled_only,
        low_sample_threshold=low_sample_threshold,
        now=now,
    )
    report["database_verification"] = {**verification, "ok": True}
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate event-aware forecast reliability report artifacts"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--json", type=Path, default=Path("event_aware_evaluation.json"))
    parser.add_argument("--calendar", type=Path, help="event calendar JSON file")
    parser.add_argument("--model-name", default=ENSEMBLE_MODEL)
    parser.add_argument("--window-hours", type=float, default=DEFAULT_WINDOW_HOURS)
    parser.add_argument(
        "--known-scheduled-only", action="store_true", default=True, dest="known_scheduled_only"
    )
    parser.add_argument(
        "--include-unscheduled",
        action="store_false",
        dest="known_scheduled_only",
        help="also segment against events whose timing was not announced in advance",
    )
    parser.add_argument("--low-sample-threshold", type=int, default=DEFAULT_LOW_SAMPLE_THRESHOLD)
    args = parser.parse_args()
    report = generate_report(
        args.db,
        json_path=args.json,
        calendar_path=args.calendar,
        model_name=args.model_name,
        window_hours=args.window_hours,
        known_scheduled_only=args.known_scheduled_only,
        low_sample_threshold=args.low_sample_threshold,
    )
    print(
        json.dumps(
            {
                "generated_at": report["generated_at"],
                "evaluated_rows": report["evaluated_rows"],
                "segments": report["segments"],
                "json": str(args.json),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
