#!/usr/bin/env python3
"""Leakage-safe model-error and market-feature drift detection.

Production evaluates drift before creating a new forecast. Forecast-error signals
consume only matured durable outcomes; feature signals consume only already
observed completed-candle features, optionally including the current completed
candle. Thresholds and rolling windows are deterministic and included in every
report so an alert can be reproduced later.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np


REPORT_PATH = Path("drift_report.json")
SUMMARY_PATH = Path("drift_summary.md")
REPORT_SCHEMA_VERSION = 1
SEVERITY_ORDER = {"none": 0, "warning": 1, "severe": 2}
DEFAULT_FEATURES = (
    "volatility_24h_pct",
    "range_24h_avg_pct",
    "volume_zscore_7d",
    "rsi_14",
    "momentum_24h_pct",
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class DriftConfig:
    """Deterministic rolling windows and drift thresholds."""

    error_recent: int = 8
    error_baseline: int = 24
    feature_recent: int = 12
    feature_baseline: int = 36
    warning_shift_z: float = 2.0
    severe_shift_z: float = 3.5
    warning_ks: float = 0.35
    severe_ks: float = 0.55
    warning_direction_drop: float = 0.15
    severe_direction_drop: float = 0.25
    warning_adaptive_confidence: float = 0.50
    severe_adaptive_confidence: float = 0.0

    @classmethod
    def from_env(cls) -> "DriftConfig":
        return cls(
            error_recent=max(2, _env_int("BTC_DRIFT_ERROR_RECENT", cls.error_recent)),
            error_baseline=max(4, _env_int("BTC_DRIFT_ERROR_BASELINE", cls.error_baseline)),
            feature_recent=max(2, _env_int("BTC_DRIFT_FEATURE_RECENT", cls.feature_recent)),
            feature_baseline=max(4, _env_int("BTC_DRIFT_FEATURE_BASELINE", cls.feature_baseline)),
            warning_shift_z=max(0.1, _env_float("BTC_DRIFT_WARNING_SHIFT_Z", cls.warning_shift_z)),
            severe_shift_z=max(0.1, _env_float("BTC_DRIFT_SEVERE_SHIFT_Z", cls.severe_shift_z)),
            warning_ks=min(1.0, max(0.01, _env_float("BTC_DRIFT_WARNING_KS", cls.warning_ks))),
            severe_ks=min(1.0, max(0.01, _env_float("BTC_DRIFT_SEVERE_KS", cls.severe_ks))),
            warning_direction_drop=min(
                1.0,
                max(
                    0.0,
                    _env_float("BTC_DRIFT_WARNING_DIRECTION_DROP", cls.warning_direction_drop),
                ),
            ),
            severe_direction_drop=min(
                1.0,
                max(
                    0.0,
                    _env_float("BTC_DRIFT_SEVERE_DIRECTION_DROP", cls.severe_direction_drop),
                ),
            ),
            warning_adaptive_confidence=min(
                1.0,
                max(
                    0.0,
                    _env_float(
                        "BTC_DRIFT_WARNING_ADAPTIVE_CONFIDENCE",
                        cls.warning_adaptive_confidence,
                    ),
                ),
            ),
            severe_adaptive_confidence=min(
                1.0,
                max(
                    0.0,
                    _env_float(
                        "BTC_DRIFT_SEVERE_ADAPTIVE_CONFIDENCE",
                        cls.severe_adaptive_confidence,
                    ),
                ),
            ),
        )

    def validate(self) -> None:
        if self.severe_shift_z < self.warning_shift_z:
            raise ValueError("severe shift threshold must be >= warning threshold")
        if self.severe_ks < self.warning_ks:
            raise ValueError("severe KS threshold must be >= warning threshold")
        if self.severe_direction_drop < self.warning_direction_drop:
            raise ValueError("severe direction-drop threshold must be >= warning threshold")


def _as_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _max_severity(*values: str) -> str:
    return max(values, key=lambda value: SEVERITY_ORDER.get(value, -1), default="none")


def _ks_distance(baseline: np.ndarray, recent: np.ndarray) -> float:
    """Return the exact two-sample empirical CDF distance without SciPy."""
    if baseline.size == 0 or recent.size == 0:
        return 0.0
    base_sorted = np.sort(baseline.astype(np.float64))
    recent_sorted = np.sort(recent.astype(np.float64))
    points = np.unique(np.concatenate((base_sorted, recent_sorted)))
    base_cdf = np.searchsorted(base_sorted, points, side="right") / base_sorted.size
    recent_cdf = np.searchsorted(recent_sorted, points, side="right") / recent_sorted.size
    return float(np.max(np.abs(base_cdf - recent_cdf)))


def _distribution_metrics(
    baseline_values: Iterable[float], recent_values: Iterable[float]
) -> dict[str, Any]:
    baseline = np.asarray(list(baseline_values), dtype=np.float64)
    recent = np.asarray(list(recent_values), dtype=np.float64)
    base_median = float(np.median(baseline))
    recent_median = float(np.median(recent))
    mad = float(np.median(np.abs(baseline - base_median)))
    robust_scale = max(1.4826 * mad, abs(base_median) * 0.10, 1e-6)
    shift_z = abs(recent_median - base_median) / robust_scale
    return {
        "baseline_samples": int(baseline.size),
        "recent_samples": int(recent.size),
        "baseline_median": round(base_median, 6),
        "recent_median": round(recent_median, 6),
        "median_change": round(recent_median - base_median, 6),
        "robust_scale": round(robust_scale, 6),
        "robust_shift_z": round(float(shift_z), 6),
        "ks_distance": round(_ks_distance(baseline, recent), 6),
    }


def _distribution_severity(metrics: Mapping[str, Any], config: DriftConfig) -> str:
    shift_z = float(metrics.get("robust_shift_z", 0.0))
    ks_distance = float(metrics.get("ks_distance", 0.0))
    if shift_z >= config.severe_shift_z or ks_distance >= config.severe_ks:
        return "severe"
    if shift_z >= config.warning_shift_z or ks_distance >= config.warning_ks:
        return "warning"
    return "none"


def _direction_severity(drop: float, config: DriftConfig) -> str:
    if drop >= config.severe_direction_drop:
        return "severe"
    if drop >= config.warning_direction_drop:
        return "warning"
    return "none"


def _error_signals(rows: Iterable[Mapping[str, Any]], config: DriftConfig) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    for row in rows:
        absolute_error = _as_float(row.get("absolute_error_pct"))
        signed_error = _as_float(row.get("signed_error_pct"))
        if absolute_error is None or signed_error is None:
            # Missing error fields mean the prediction is not matured and cannot
            # participate in a production drift decision.
            continue
        try:
            model_name = str(row["model_name"])
            horizon = int(row["horizon_hours"])
        except (KeyError, TypeError, ValueError):
            continue
        grouped.setdefault((model_name, horizon), []).append(row)

    needed = config.error_baseline + config.error_recent
    signals: list[dict[str, Any]] = []
    for (model_name, horizon), values in sorted(grouped.items()):
        ordered = sorted(values, key=lambda item: str(item.get("target_at", "")))
        if len(ordered) < needed:
            continue
        window = ordered[-needed:]
        baseline_rows = window[: config.error_baseline]
        recent_rows = window[config.error_baseline :]
        baseline_abs = [float(row["absolute_error_pct"]) for row in baseline_rows]
        recent_abs = [float(row["absolute_error_pct"]) for row in recent_rows]
        baseline_signed = [float(row["signed_error_pct"]) for row in baseline_rows]
        recent_signed = [float(row["signed_error_pct"]) for row in recent_rows]
        absolute_metrics = _distribution_metrics(baseline_abs, recent_abs)
        signed_metrics = _distribution_metrics(baseline_signed, recent_signed)

        baseline_direction = [
            float(row["direction_correct"])
            for row in baseline_rows
            if row.get("direction_correct") is not None
        ]
        recent_direction = [
            float(row["direction_correct"])
            for row in recent_rows
            if row.get("direction_correct") is not None
        ]
        baseline_accuracy = float(np.mean(baseline_direction)) if baseline_direction else None
        recent_accuracy = float(np.mean(recent_direction)) if recent_direction else None
        direction_drop = (
            max(0.0, baseline_accuracy - recent_accuracy)
            if baseline_accuracy is not None and recent_accuracy is not None
            else 0.0
        )

        absolute_severity = _distribution_severity(absolute_metrics, config)
        signed_severity = _distribution_severity(signed_metrics, config)
        direction_severity = _direction_severity(direction_drop, config)
        severity = _max_severity(absolute_severity, signed_severity, direction_severity)
        signals.append(
            {
                "signal_key": f"error:{model_name}:{horizon}h",
                "kind": "model_error",
                "severity": severity,
                "model_name": model_name,
                "horizon_hours": horizon,
                "feature_name": None,
                "baseline_start_at": baseline_rows[0].get("target_at"),
                "baseline_end_at": baseline_rows[-1].get("target_at"),
                "recent_start_at": recent_rows[0].get("target_at"),
                "recent_end_at": recent_rows[-1].get("target_at"),
                "metrics": {
                    "absolute_error_pct": absolute_metrics,
                    "signed_error_pct": signed_metrics,
                    "baseline_direction_accuracy": (
                        round(baseline_accuracy, 6) if baseline_accuracy is not None else None
                    ),
                    "recent_direction_accuracy": (
                        round(recent_accuracy, 6) if recent_accuracy is not None else None
                    ),
                    "direction_accuracy_drop": round(direction_drop, 6),
                },
            }
        )
    return signals


def _normalized_feature_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    current_features: Mapping[str, Any] | None,
    current_origin_at: str | None,
) -> list[tuple[str, Mapping[str, Any]]]:
    normalized: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        origin = row.get("origin_at")
        features = row.get("market_features")
        if origin and isinstance(features, Mapping):
            normalized[str(origin)] = features
    if current_features is not None and current_origin_at:
        normalized[str(current_origin_at)] = current_features
    return sorted(normalized.items(), key=lambda item: item[0])


def _feature_signals(
    rows: Iterable[Mapping[str, Any]],
    config: DriftConfig,
    *,
    features: Iterable[str],
    current_features: Mapping[str, Any] | None,
    current_origin_at: str | None,
) -> list[dict[str, Any]]:
    normalized = _normalized_feature_rows(
        rows,
        current_features=current_features,
        current_origin_at=current_origin_at,
    )
    needed = config.feature_baseline + config.feature_recent
    signals: list[dict[str, Any]] = []
    for feature_name in features:
        values = [
            (origin, value)
            for origin, feature_map in normalized
            if (value := _as_float(feature_map.get(feature_name))) is not None
        ]
        if len(values) < needed:
            continue
        window = values[-needed:]
        baseline = window[: config.feature_baseline]
        recent = window[config.feature_baseline :]
        metrics = _distribution_metrics(
            [value for _, value in baseline],
            [value for _, value in recent],
        )
        severity = _distribution_severity(metrics, config)
        signals.append(
            {
                "signal_key": f"feature:{feature_name}",
                "kind": "market_feature",
                "severity": severity,
                "model_name": None,
                "horizon_hours": None,
                "feature_name": feature_name,
                "baseline_start_at": baseline[0][0],
                "baseline_end_at": baseline[-1][0],
                "recent_start_at": recent[0][0],
                "recent_end_at": recent[-1][0],
                "metrics": metrics,
            }
        )
    return signals


def adaptive_confidence_for_severity(severity: str, config: DriftConfig) -> float:
    if severity == "severe":
        return config.severe_adaptive_confidence
    if severity == "warning":
        return config.warning_adaptive_confidence
    return 1.0


def evaluate_drift(
    prediction_rows: Iterable[Mapping[str, Any]],
    feature_rows: Iterable[Mapping[str, Any]],
    *,
    current_features: Mapping[str, Any] | None = None,
    current_origin_at: str | None = None,
    config: DriftConfig | None = None,
    features: Iterable[str] = DEFAULT_FEATURES,
    evaluated_at: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate model/error and feature drift without using future outcomes."""
    prediction_rows = list(prediction_rows)
    feature_rows = list(feature_rows)
    active = config or DriftConfig.from_env()
    active.validate()
    error_signals = _error_signals(prediction_rows, active)
    feature_signals = _feature_signals(
        feature_rows,
        active,
        features=features,
        current_features=current_features,
        current_origin_at=current_origin_at,
    )
    all_signals = [*error_signals, *feature_signals]
    severity = _max_severity(*(str(signal["severity"]) for signal in all_signals))
    events = [signal for signal in all_signals if signal["severity"] != "none"]
    warning_count = sum(signal["severity"] == "warning" for signal in events)
    severe_count = sum(signal["severity"] == "severe" for signal in events)
    timestamp = evaluated_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    timestamp = timestamp.astimezone(timezone.utc)

    origins = [str(row.get("origin_at")) for row in feature_rows if row.get("origin_at")]
    if current_origin_at:
        origins.append(str(current_origin_at))
    latest_origin = max(origins) if origins else None
    confidence = adaptive_confidence_for_severity(severity, active)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "evaluated_at": timestamp.isoformat(),
        "latest_observed_origin_at": latest_origin,
        "severity": severity,
        "adaptive_confidence": round(confidence, 4),
        "fallback_mode": "static_prior" if severity == "severe" and confidence <= 0 else None,
        "configuration": asdict(active),
        "features_monitored": list(features),
        "summary": {
            "signals_evaluated": len(all_signals),
            "events": len(events),
            "warnings": warning_count,
            "severe": severe_count,
        },
        "error_signals": error_signals,
        "feature_signals": feature_signals,
        "events": events,
    }


def render_drift_summary(report: Mapping[str, Any]) -> str:
    severity = str(report.get("severity", "none")).upper()
    summary = report.get("summary", {})
    confidence = float(report.get("adaptive_confidence", 1.0))
    lines = [
        "## Production drift detection",
        "",
        f"- Overall state: **{severity}**",
        f"- Adaptive-weight confidence: **{confidence:.2f}**",
        f"- Evaluated signals: **{int(summary.get('signals_evaluated', 0))}**",
        f"- Drift events: **{int(summary.get('events', 0))}**",
    ]
    fallback = report.get("fallback_mode")
    if fallback:
        lines.append(f"- Production fallback: **{fallback}**")
    events = report.get("events", [])
    if isinstance(events, list) and events:
        lines.extend(
            [
                "",
                "| Signal | Severity | Shift z | KS | Direction drop |",
                "| --- | --- | ---: | ---: | ---: |",
            ]
        )
        for event in events:
            metrics = event.get("metrics", {}) if isinstance(event, Mapping) else {}
            if event.get("kind") == "model_error":
                absolute = metrics.get("absolute_error_pct", {})
                shift_z = float(absolute.get("robust_shift_z", 0.0))
                ks = float(absolute.get("ks_distance", 0.0))
                direction_drop = float(metrics.get("direction_accuracy_drop", 0.0))
            else:
                shift_z = float(metrics.get("robust_shift_z", 0.0))
                ks = float(metrics.get("ks_distance", 0.0))
                direction_drop = 0.0
            lines.append(
                f"| `{event.get('signal_key')}` | {str(event.get('severity')).upper()} | "
                f"{shift_z:.2f} | {ks:.2f} | {direction_drop:.2f} |"
            )
    return "\n".join(lines) + "\n"


def persist_drift_report(
    report: Mapping[str, Any],
    *,
    report_path: Path = REPORT_PATH,
    summary_path: Path = SUMMARY_PATH,
) -> None:
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary_path.write_text(render_drift_summary(report), encoding="utf-8")
