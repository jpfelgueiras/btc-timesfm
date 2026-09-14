#!/usr/bin/env python3
"""Feature freshness and decay analysis.

Measures how predictive value changes as each external or engineered signal
ages. For every feature we derive a deterministic age in hours from the
forecast origin and record its observation time, bucket matured forecasts by
feature-age bands, and report value-vs-age curves per horizon. Stale thresholds
are chosen from the curves (no look-ahead) and the resulting recommendations are
shaped for the feature-selection pipeline.

All thresholds and policies are deterministic. A feature is classified as
``fresh`` until its age exceeds the (evidence-based or default) threshold, as
``stale_downgrade`` once past it, and as ``stale_exclude`` only when the value is
missing outright or an explicit exclusion age is supplied. Rows whose target
candle lies after the evaluation ``now`` are always excluded so a value-vs-age
curve can never use a future outcome.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from btc_timesfm.history.history_store import (
    DEFAULT_DB_PATH,
    ENSEMBLE_MODEL,
    ForecastHistoryStore,
)

SCHEMA_VERSION = 1
DEFAULT_HORIZONS = (2, 4, 8, 16)
DEFAULT_MIN_SAMPLES = 20
DEFAULT_DEGRADATION_TOLERANCE = 0.05
DEFAULT_REPORT_PATH = Path("feature_freshness_report.json")
DEFAULT_SUMMARY_PATH = Path("feature_freshness_report.md")

# Age-band edges in hours; the last band extends to infinity.
DEFAULT_AGE_BAND_EDGES_HOURS = (0.0, 1.0, 2.0, 4.0, 8.0, 12.0, 24.0, 48.0, 72.0, 168.0)

# Reserved keys inside a features dict that carry capture metadata, not values.
FEATURE_OBSERVATION_TIME_KEYS = ("feature_observation_times", "feature_timestamps")
GLOBAL_CAPTURE_TIME_KEYS = ("captured_at", "observation_at")
RESERVED_FEATURE_KEYS = frozenset(FEATURE_OBSERVATION_TIME_KEYS + GLOBAL_CAPTURE_TIME_KEYS)

# Deterministic default stale thresholds (hours), aligned with the provider
# staleness rules used by each signal module when it emits a feature.
FALLBACK_DEFAULT_THRESHOLD_HOURS = 1.0
FEATURE_PREFIX_DEFAULT_THRESHOLD_HOURS = (
    ("derivatives_funding", 12.0),
    ("derivatives_", 2.5),
    ("microstructure_", 1.25),
    ("cross_eth_", 2.0),
    ("cross_btc_eth_", 2.0),
    ("macro_", 168.0),
)
DEFAULT_STALE_THRESHOLD_HOURS: dict[str, float] = {}

INF = float("inf")


def _as_utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_observation_time(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            return _as_utc(value)
        except ValueError:
            return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = float(value)
    if seconds > 10**12:
        seconds /= 1000.0
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _is_feature_value(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None else None


def _fmt_hours(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:g}"


def _band_label(start: float, end: float) -> str:
    if math.isinf(end):
        return f"{_fmt_hours(start)}h+"
    return f"{_fmt_hours(start)}-{_fmt_hours(end)}h"


def default_stale_threshold_hours(
    feature_name: str,
    overrides: Mapping[str, float] | None = None,
) -> float:
    """Return the deterministic default stale threshold for a feature name."""
    if overrides and feature_name in overrides:
        return float(overrides[feature_name])
    if feature_name in DEFAULT_STALE_THRESHOLD_HOURS:
        return float(DEFAULT_STALE_THRESHOLD_HOURS[feature_name])
    for prefix, hours in FEATURE_PREFIX_DEFAULT_THRESHOLD_HOURS:
        if feature_name.startswith(prefix):
            return float(hours)
    return FALLBACK_DEFAULT_THRESHOLD_HOURS


def feature_ages(
    features_dict: Mapping[str, Any],
    origin_at: datetime | str,
) -> dict[str, float]:
    """Compute each feature's age in hours relative to the forecast origin.

    Observation times are read deterministically from the features dict:

    - a per-feature map stored under ``feature_observation_times`` (or the
      alias ``feature_timestamps``), mapping feature name to an ISO timestamp
      or epoch seconds;
    - otherwise a global ``captured_at``/``observation_at`` timestamp applied to
      every feature that has no per-feature time.

    Only numeric features with a recorded observation time are returned.
    Features without a recorded observation time are omitted so callers can
    tell that their freshness was never recorded.
    """
    origin = _as_utc(origin_at)
    per_feature_times: dict[str, datetime] = {}
    for key in FEATURE_OBSERVATION_TIME_KEYS:
        raw = features_dict.get(key)
        if not isinstance(raw, dict):
            continue
        for name, timestamp in raw.items():
            observed = _parse_observation_time(timestamp)
            if observed is not None:
                per_feature_times[str(name)] = observed

    capture = None
    for key in GLOBAL_CAPTURE_TIME_KEYS:
        parsed = _parse_observation_time(features_dict.get(key))
        if parsed is not None:
            capture = parsed
            break

    ages: dict[str, float] = {}
    for name, value in features_dict.items():
        if name in RESERVED_FEATURE_KEYS or not _is_feature_value(value):
            continue
        observed = per_feature_times.get(name) or capture
        if observed is None:
            continue
        age_hours = max(0.0, (origin - observed).total_seconds() / 3600.0)
        ages[str(name)] = round(age_hours, 6)
    return ages


def _features_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    features = row.get("market_features")
    if isinstance(features, dict):
        return dict(features)
    raw = row.get("market_features_json")
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _horizon_hours(row: Mapping[str, Any]) -> int | None:
    value = row.get("horizon_hours")
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value.rstrip("h"))
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None
    return None


def _model_name(row: Mapping[str, Any]) -> str | None:
    value = row.get("model_name")
    return str(value) if value is not None and not isinstance(value, bool) else None


def _normalize_samples(
    rows: Iterable[Mapping[str, Any]],
    now: datetime | str | None,
    horizons: Sequence[int],
) -> tuple[list[dict[str, Any]], int]:
    origin: datetime | None = None
    cutoff: datetime | None = None
    if now is not None:
        cutoff = _as_utc(now)

    horizon_set = {int(hour) for hour in horizons}
    samples: list[dict[str, Any]] = []
    excluded_future = 0

    for row in rows:
        absolute_error = _finite_float(row.get("absolute_error_pct"))
        if absolute_error is None:
            continue
        try:
            origin = _as_utc(str(row.get("origin_at") or ""))
        except ValueError:
            continue
        hour = _horizon_hours(row)
        if hour is None or hour not in horizon_set:
            continue

        target = None
        raw_target = row.get("target_at")
        if isinstance(raw_target, str) and raw_target:
            try:
                target = _as_utc(raw_target)
            except ValueError:
                target = None
        if target is None:
            target = origin + timedelta(hours=hour)
        if cutoff is not None and target > cutoff:
            excluded_future += 1
            continue

        features = _features_from_row(row)
        direction = _finite_float(row.get("direction_correct"))
        actual_change = _finite_float(row.get("actual_change_pct"))
        samples.append(
            {
                "origin_at": origin.isoformat(),
                "horizon_hours": hour,
                "model_name": _model_name(row),
                "features": {k: v for k, v in features.items() if _is_feature_value(v)},
                "ages": feature_ages(features, origin),
                "absolute_error_pct": absolute_error,
                "direction_correct": direction,
                "actual_change_pct": actual_change,
            }
        )
    return samples, excluded_future


def _select_model_samples(
    samples: list[dict[str, Any]],
    model_name: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    names = {sample["model_name"] for sample in samples if sample["model_name"]}
    effective = model_name
    if effective is None and ENSEMBLE_MODEL in names:
        effective = ENSEMBLE_MODEL
    if effective is None:
        return samples, None
    return [sample for sample in samples if sample["model_name"] == effective], effective


def _band_metrics(
    entries: list[dict[str, Any]],
    *,
    min_samples: int,
    edges: Sequence[float],
) -> list[dict[str, Any]]:
    buckets: dict[int, list[dict[str, Any]]] = {index: [] for index in range(len(edges))}
    for entry in entries:
        for index in range(len(edges) - 1):
            end = edges[index + 1]
            if entry["age_hours"] < end:
                buckets[index].append(entry)
                break
        else:
            buckets[len(edges) - 1].append(entry)

    bands: list[dict[str, Any]] = []
    for index in range(len(edges)):
        start = edges[index]
        end = edges[index + 1] if index + 1 < len(edges) else INF
        band_entries = buckets[index]
        conclusive = len(band_entries) >= min_samples
        mae = (
            sum(float(e["absolute_error_pct"]) for e in band_entries) / len(band_entries)
            if conclusive and band_entries
            else None
        )
        directions = [d for e in band_entries if (d := e["direction_correct"]) is not None]
        direction_accuracy = (
            sum(directions) / len(directions) if conclusive and directions else None
        )
        baseline = [b for e in band_entries if (b := e["actual_change_pct"]) is not None]
        baseline_mae = (
            sum(abs(b) for b in baseline) / len(baseline) if conclusive and baseline else None
        )
        bands.append(
            {
                "age_band": _band_label(start, end),
                "age_band_start_hours": round(start, 6),
                "age_band_end_hours": round(end, 6) if not math.isinf(end) else None,
                "samples": len(band_entries),
                "mae_pct": _round(mae),
                "direction_accuracy": _round(direction_accuracy),
                "baseline_mae_pct": _round(baseline_mae),
                "conclusive": conclusive,
            }
        )
    return bands


def evaluate_value_vs_age(
    rows: Iterable[Mapping[str, Any]],
    *,
    now: datetime | str | None = None,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    age_band_edges: Sequence[float] = DEFAULT_AGE_BAND_EDGES_HOURS,
    model_name: str | None = None,
) -> tuple[dict[str, dict[str, dict[str, Any]]], str | None]:
    """Bucket matured forecasts by feature-age bands and report per-horizon curves.

    Leakage-safe: any row whose target candle is after ``now`` is excluded, and
    per-horizon metrics use only the feature values that were available at the
    forecast origin. Returns ``(curves, effective_model)`` where ``curves`` maps
    feature -> horizon -> curve.
    """
    if min_samples < 1:
        raise ValueError("min_samples must be >= 1")
    horizons = tuple(int(hour) for hour in horizons)
    if not horizons or any(hour <= 0 for hour in horizons):
        raise ValueError("horizons must be positive hours")

    samples, _ = _normalize_samples(rows, now, horizons)
    samples, effective_model = _select_model_samples(samples, model_name)
    edges = tuple(sorted(float(edge) for edge in age_band_edges))
    if not edges or any(b <= a for a, b in zip(edges, edges[1:])):
        raise ValueError("age band edges must be strictly increasing")

    per_feature: dict[str, dict[int, list[dict[str, Any]]]] = {}
    for sample in samples:
        for feature, age_hours in sample["ages"].items():
            if feature not in sample["features"]:
                continue
            per_feature.setdefault(feature, {})
            per_feature[feature].setdefault(sample["horizon_hours"], []).append(
                {
                    "age_hours": age_hours,
                    "absolute_error_pct": sample["absolute_error_pct"],
                    "direction_correct": sample["direction_correct"],
                    "actual_change_pct": sample["actual_change_pct"],
                }
            )

    curves: dict[str, dict[str, dict[str, Any]]] = {}
    for feature in sorted(per_feature):
        curves[feature] = {}
        for hour in sorted(per_feature[feature]):
            entries = per_feature[feature][hour]
            bands = _band_metrics(entries, min_samples=min_samples, edges=edges)
            mae = (
                sum(float(e["absolute_error_pct"]) for e in entries) / len(entries)
                if len(entries) >= min_samples and entries
                else None
            )
            directions = [d for e in entries if (d := e["direction_correct"]) is not None]
            direction_accuracy = (
                sum(directions) / len(directions)
                if len(entries) >= min_samples and directions
                else None
            )
            baseline = [b for e in entries if (b := e["actual_change_pct"]) is not None]
            baseline_mae = (
                sum(abs(b) for b in baseline) / len(baseline)
                if len(entries) >= min_samples and baseline
                else None
            )
            curves[feature][f"{hour}h"] = {
                "samples": len(entries),
                "mae_pct": _round(mae),
                "direction_accuracy": _round(direction_accuracy),
                "baseline_mae_pct": _round(baseline_mae),
                "curve": bands,
            }
    return curves, effective_model


def _band_status(band: Mapping[str, Any], tolerance: float) -> str:
    mae = band.get("mae_pct")
    baseline = band.get("baseline_mae_pct")
    if not band.get("conclusive") or mae is None or baseline is None:
        return "insufficient"
    base = float(baseline)
    if base <= 0:
        return "neutral"
    if float(mae) < base:
        return "improving"
    if float(mae) <= base * (1.0 + max(0.0, tolerance)):
        return "neutral"
    return "degraded"


def evidence_based_threshold(
    curve: Sequence[Mapping[str, Any]],
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    tolerance: float = DEFAULT_DEGRADATION_TOLERANCE,
    default_threshold_hours: float,
) -> dict[str, Any]:
    """Derive a horizon-specific stale threshold from a value-vs-age curve.

    The threshold is the lower edge of the first age band (walking from youngest
    to oldest) where the feature stops improving over the neutral/persistence
    baseline. Staleness is treated as cumulative: once a band stops improving,
    older values are considered stale even if a later band improves again. If
    the evidence is insufficient the deterministic default threshold is kept.
    """
    if min_samples < 1:
        raise ValueError("min_samples must be >= 1")
    if not 0.0 <= tolerance < 1.0:
        raise ValueError("tolerance must be between 0 and 1")

    definitive = [
        band
        for band in curve
        if band.get("conclusive")
        and int(band.get("samples", 0)) >= min_samples
        and band.get("mae_pct") is not None
        and band.get("baseline_mae_pct") is not None
    ]
    evidence_samples = sum(int(band.get("samples", 0)) for band in definitive)
    band_statuses = [
        {
            "age_band": band.get("age_band"),
            "age_band_start_hours": band.get("age_band_start_hours"),
            "samples": int(band.get("samples", 0)),
            "mae_pct": _round(band.get("mae_pct")),
            "baseline_mae_pct": _round(band.get("baseline_mae_pct")),
            "status": _band_status(band, tolerance),
        }
        for band in curve
        if band in definitive
    ]

    state = "none"
    threshold: float | None = None
    action = "keep"
    questionable: Mapping[str, Any] | None = None

    for band in definitive:
        status = _band_status(band, tolerance)
        if status == "insufficient":
            continue
        if state == "none" and status == "improving":
            state = "improving"
        elif state == "improving" and status != "improving":
            threshold = float(band["age_band_start_hours"])
            action = "exclude" if status == "degraded" else "downgrade"
            questionable = band
            state = status

    if threshold is None:
        if state == "improving":
            last = definitive[-1]
            end = last.get("age_band_end_hours")
            threshold = (
                float(end)
                if end is not None and math.isfinite(float(end))
                else float(last["age_band_start_hours"])
            )
            action = "keep"
            reason = "no_aged_region_degraded_within_observed_range"
            evidence_based = True
        else:
            threshold = float(default_threshold_hours)
            action = "keep"
            reason = "insufficient_evidence_of_value_by_age_band"
            evidence_based = False
    else:
        reason = "first_age_band_without_persistence_edge"
        evidence_based = True

    return {
        "threshold_hours": round(float(threshold), 6),
        "default_threshold_hours": round(float(default_threshold_hours), 6),
        "evidence_based": evidence_based,
        "action": action,
        "questionable_age_band": (
            str(questionable.get("age_band")) if questionable is not None else None
        ),
        "evidence_samples": evidence_samples,
        "questionable_samples": (
            int(questionable.get("samples", 0)) if questionable is not None else None
        ),
        "reason": reason,
        "band_statuses": band_statuses,
    }


def stale_signal_policy(
    feature_value: Any,
    feature_age_hours: float | None,
    threshold_hours: float,
    *,
    exclude_age_hours: float | None = None,
) -> str:
    """Map a feature value and its age to a deterministic stale policy.

    Returns one of ``"fresh"`` | ``"stale_downgrade"`` | ``"stale_exclude"``.

    - Missing values are always ``stale_exclude``.
    - An unrecorded age downgrades the value conservatively.
    - Values at or below the threshold are ``"fresh"``.
    - Values past the threshold are ``"stale_downgrade"``, unless an explicit
      ``exclude_age_hours`` is supplied and exceeded, in which case they are
      ``"stale_exclude"``.
    """
    if feature_value is None:
        return "stale_exclude"
    threshold = float(threshold_hours)
    if not math.isfinite(threshold) or threshold < 0:
        raise ValueError("threshold_hours must be a non-negative finite number")
    if feature_age_hours is None:
        return "stale_downgrade"
    if exclude_age_hours is not None:
        if not math.isfinite(float(exclude_age_hours)) or float(exclude_age_hours) < 0:
            raise ValueError("exclude_age_hours must be a non-negative finite number")
        if feature_age_hours > float(exclude_age_hours):
            return "stale_exclude"
    if feature_age_hours > threshold:
        return "stale_downgrade"
    return "fresh"


def _feature_group(feature_name: str) -> str:
    if feature_name.startswith("derivatives_"):
        return "derivatives"
    if feature_name.startswith("microstructure_"):
        return "microstructure"
    if feature_name.startswith(("cross_", "macro_")):
        return "cross_asset"
    return "market"


def build_freshness_report(
    rows: Iterable[Mapping[str, Any]],
    now: datetime | str,
    *,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    degradation_tolerance: float = DEFAULT_DEGRADATION_TOLERANCE,
    age_band_edges: Sequence[float] = DEFAULT_AGE_BAND_EDGES_HOURS,
    model_name: str | None = None,
    default_thresholds: Mapping[str, float] | None = None,
    generated_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Build a JSON-serializable feature freshness report from matured rows.

    ``rows`` use the same shape as ``ForecastHistoryStore.export_rows()``. The
    report records which rows were excluded for leaking future outcomes and
    exposes the deterministic missing/stale policy for the selection pipeline.
    """
    if min_samples < 1:
        raise ValueError("min_samples must be >= 1")
    if not 0.0 <= degradation_tolerance < 1.0:
        raise ValueError("degradation_tolerance must be between 0 and 1")
    horizons = tuple(int(hour) for hour in horizons)
    if not horizons or any(hour <= 0 for hour in horizons):
        raise ValueError("horizons must be positive hours")

    now_timezone = _as_utc(now)
    rows_list = list(rows)
    samples, excluded_future = _normalize_samples(rows_list, now_timezone, horizons)
    selected, effective_model = _select_model_samples(samples, model_name)
    curves, _ = evaluate_value_vs_age(
        rows_list,
        now=now_timezone,
        horizons=horizons,
        min_samples=min_samples,
        age_band_edges=age_band_edges,
        model_name=effective_model,
    )

    matured_rows = sum(
        1 for row in rows_list if _finite_float(row.get("absolute_error_pct")) is not None
    )
    horizon_samples: dict[str, int] = {}
    for hour in horizons:
        horizon_samples[f"{hour}h"] = sum(
            1 for sample in selected if sample["horizon_hours"] == hour
        )

    feature_names = sorted(curves)
    feature_groups = {feature: _feature_group(feature) for feature in feature_names}
    default_by_feature = {
        feature: default_stale_threshold_hours(feature, default_thresholds)
        for feature in feature_names
    }

    stale_thresholds: dict[str, dict[str, Any]] = {}
    recommendations: list[dict[str, Any]] = []
    for feature in feature_names:
        stale_thresholds[feature] = {}
        for hour in horizons:
            horizon_key = f"{hour}h"
            curve = curves[feature].get(horizon_key, {}).get("curve", [])
            default_threshold = default_by_feature[feature]
            evidence = evidence_based_threshold(
                curve,
                min_samples=min_samples,
                tolerance=degradation_tolerance,
                default_threshold_hours=default_threshold,
            )
            stale_thresholds[feature][horizon_key] = evidence
            recommendations.append(
                {
                    "feature": feature,
                    "feature_group": feature_groups[feature],
                    "horizon": horizon_key,
                    "threshold_hours": evidence["threshold_hours"],
                    "default_threshold_hours": evidence["default_threshold_hours"],
                    "evidence_samples": evidence["evidence_samples"],
                    "questionable_samples": evidence["questionable_samples"],
                    "action": evidence["action"],
                    "evidence_based": evidence["evidence_based"],
                    "reason": evidence["reason"],
                }
            )

    recommendations.sort(key=lambda item: (item["feature"], item["horizon"]))

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": (
            (_as_utc(generated_at) if generated_at else datetime.now(timezone.utc))
            .astimezone(timezone.utc)
            .isoformat()
        ),
        "now": now_timezone.isoformat(),
        "horizons": [f"{hour}h" for hour in horizons],
        "model_name": effective_model,
        "min_samples": min_samples,
        "degradation_tolerance": round(degradation_tolerance, 6),
        "age_band_edges_hours": [round(float(edge), 6) for edge in age_band_edges],
        "samples": {
            "total_rows": len(rows_list),
            "matured_rows": matured_rows,
            "evaluation_rows": len(selected),
            "excluded_future_rows": excluded_future,
            "by_horizon": horizon_samples,
        },
        "default_stale_threshold_hours": {
            feature: round(default_by_feature[feature], 6) for feature in feature_names
        },
        "feature_groups": feature_groups,
        "curves": curves,
        "stale_thresholds": stale_thresholds,
        "recommendations": recommendations,
        "policy": {
            "stale_signal_policy": "fresh | stale_downgrade | stale_exclude",
            "missing_feature_action": "stale_exclude",
            "unrecorded_age_action": "stale_downgrade",
            "exclusion_beyond_default": "stale_downgrade",
            "explicit_exclusion_age_hours": "stale_exclude",
        },
        "leakage_safety": {
            "method": "exclude_rows_with_target_at_after_now",
            "now": now_timezone.isoformat(),
            "excluded_future_rows": excluded_future,
        },
        "source": "durable_forecast_history",
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Feature freshness and decay analysis",
        "",
        f"Generated: `{report['generated_at']}`",
        f"Leakage cutoff (`now`): `{report['now']}`",
        f"Matured rows: **{report['samples']['matured_rows']}** | "
        f"Evaluation rows: **{report['samples']['evaluation_rows']}** | "
        f"Excluded future rows: **{report['samples']['excluded_future_rows']}**",
        f"Model: **{report['model_name'] or 'all'}** | "
        f"Min samples per band: **{report['min_samples']}**",
        "",
        "Staleness is cumulative: once a feature stops improving over the "
        "neutral/persistence baseline in an age band, older values are treated "
        "as stale. Rows whose target lies after the evaluation cutoff are never "
        "used, so curves cannot leak future outcomes.",
        "",
    ]

    recs = report.get("recommendations", [])
    lines.extend(
        [
            "## Recommended stale thresholds",
            "",
            "| Feature | Group | Horizon | Samples | MAE% | Dir accuracy | Baseline MAE% | "
            "Threshold (h) | Evidence samples | Action | Evidence-based |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for rec in recs:
        curve = report["curves"].get(rec["feature"], {}).get(rec["horizon"], {})
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{rec['feature']}`",
                    rec["feature_group"] or "market",
                    rec["horizon"],
                    str(curve.get("samples", 0)),
                    _fmt(curve.get("mae_pct")),
                    _fmt(curve.get("direction_accuracy")),
                    _fmt(curve.get("baseline_mae_pct")),
                    _fmt(rec["threshold_hours"]),
                    str(rec["evidence_samples"]),
                    rec["action"],
                    str(rec["evidence_based"]),
                ]
            )
            + " |"
        )

    for feature in sorted(report["curves"]):
        for horizon in sorted(report["curves"][feature], key=lambda h: int(h.rstrip("h"))):
            curve = report["curves"][feature][horizon]
            lines.extend(
                [
                    "",
                    f"### `{feature}` · {horizon}",
                    "",
                    f"Overall: samples={curve['samples']}, MAE={_fmt(curve['mae_pct'])}%, "
                    f"direction accuracy={_fmt(curve['direction_accuracy'])}, "
                    f"baseline MAE={_fmt(curve['baseline_mae_pct'])}%",
                    "",
                    "| Age band | Samples | MAE% | Dir accuracy | Baseline MAE% | Conclusive |",
                    "| --- | ---: | ---: | ---: | ---: | --- |",
                ]
            )
            for band in curve["curve"]:
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            str(band["age_band"]),
                            str(band["samples"]),
                            _fmt(band["mae_pct"]),
                            _fmt(band["direction_accuracy"]),
                            _fmt(band["baseline_mae_pct"]),
                            str(band["conclusive"]),
                        ]
                    )
                    + " |"
                )
    lines.append("")
    return "\n".join(lines)


def generate_report(
    db_path: Path,
    *,
    json_path: Path,
    markdown_path: Path,
    now: datetime | str | None = None,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    degradation_tolerance: float = DEFAULT_DEGRADATION_TOLERANCE,
    model_name: str | None = None,
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
    cutoff = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    report = build_freshness_report(
        store.export_rows(),
        cutoff,
        horizons=horizons,
        min_samples=min_samples,
        degradation_tolerance=degradation_tolerance,
        model_name=model_name,
    )
    report["database_verification"] = {**verification, "ok": True}
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate feature freshness and decay analysis artifacts"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--json", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_SUMMARY_PATH)
    parser.add_argument("--now", type=str, default=None)
    parser.add_argument(
        "--horizons",
        type=str,
        default=",".join(str(hour) for hour in DEFAULT_HORIZONS),
    )
    parser.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES)
    parser.add_argument("--tolerance", type=float, default=DEFAULT_DEGRADATION_TOLERANCE)
    parser.add_argument("--model-name", type=str, default=None)
    args = parser.parse_args()

    def _parse_horizons(raw: str) -> list[int]:
        parsed: list[int] = []
        for piece in raw.split(","):
            piece = piece.strip()
            if not piece:
                continue
            value = int(piece.rstrip("h"))
            if value > 0:
                parsed.append(value)
        return parsed

    cutoff = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else None
    report = generate_report(
        args.db,
        json_path=args.json,
        markdown_path=args.markdown,
        now=cutoff,
        horizons=_parse_horizons(args.horizons),
        min_samples=args.min_samples,
        degradation_tolerance=args.tolerance,
        model_name=args.model_name,
    )
    print(
        json.dumps(
            {
                "generated_at": report["generated_at"],
                "evaluation_rows": report["samples"]["evaluation_rows"],
                "excluded_future_rows": report["samples"]["excluded_future_rows"],
                "json": str(args.json),
                "markdown": str(args.markdown),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
