"""Timestamp-safe paired forecast evaluation across configured market segments."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from btc_timesfm.forecasting.statistical_significance import paired_bootstrap_comparison

EVALUATION_VERSION = 1
DEFAULT_SEGMENTS = (
    ("regime", "range", False),
    ("regime", "trending", False),
    ("regime", "high_volatility", True),
    ("volatility", "low", False),
    ("volatility", "normal", False),
    ("volatility", "high", True),
    ("liquidity", "low", True),
    ("liquidity", "normal", False),
    ("liquidity", "high", False),
    ("data_quality", "clean", False),
    ("data_quality", "degraded", True),
)


@dataclass(frozen=True)
class Segment:
    dimension: str
    value: str
    protected: bool = False

    @property
    def name(self) -> str:
        return f"{self.dimension}:{self.value}"


@dataclass(frozen=True)
class SegmentEvaluationPolicy:
    minimum_paired_samples: int = 8
    bootstrap_iterations: int = 2000
    maximum_protected_relative_mae_degradation: float = 0.05


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def policy_identity(policy: SegmentEvaluationPolicy, segments: Sequence[Segment]) -> str:
    payload = {
        "version": EVALUATION_VERSION,
        "policy": asdict(policy),
        "segments": [asdict(x) for x in segments],
    }
    return (
        "segment-evaluation-" + hashlib.sha256(_canonical_json(payload).encode()).hexdigest()[:16]
    )


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _segment_value(row: Mapping[str, Any], dimension: str) -> str | None:
    segments = row.get("segments")
    value = segments.get(dimension) if isinstance(segments, Mapping) else row.get(dimension)
    return str(value) if value is not None else None


def _key(row: Mapping[str, Any]) -> tuple[str, str] | None:
    origin = _timestamp(row.get("origin_at"))
    target = _timestamp(row.get("target_at"))
    if origin is None or target is None or target <= origin:
        return None
    horizon = row.get("horizon_hours")
    if horizon is None:
        return None
    return origin.isoformat(), str(horizon)


def _eligible(
    rows: Sequence[Mapping[str, Any]], now: datetime
) -> tuple[dict[tuple[str, str], Mapping[str, Any]], dict[str, int]]:
    kept: dict[tuple[str, str], Mapping[str, Any]] = {}
    excluded: dict[str, int] = {}
    for row in rows:
        key = _key(row)
        if key is None:
            excluded["invalid_or_untimestamped_row"] = (
                excluded.get("invalid_or_untimestamped_row", 0) + 1
            )
            continue
        target = _timestamp(row.get("target_at"))
        error = _number(row.get("absolute_error_pct"))
        if target is None or target > now:
            excluded["unmatured_target"] = excluded.get("unmatured_target", 0) + 1
        elif error is None:
            excluded["missing_absolute_error_pct"] = (
                excluded.get("missing_absolute_error_pct", 0) + 1
            )
        elif key in kept:
            excluded["duplicate_origin_horizon"] = excluded.get("duplicate_origin_horizon", 0) + 1
        else:
            kept[key] = row
    return kept, excluded


def evaluate_segments(
    candidate_rows: Sequence[Mapping[str, Any]],
    baseline_rows: Sequence[Mapping[str, Any]],
    *,
    segments: Sequence[Segment] | None = None,
    policy: SegmentEvaluationPolicy | None = None,
    now: datetime,
) -> dict[str, Any]:
    """Evaluate only exact origin/horizon pairs matured by ``now``.

    Segment labels must be recorded on the forecast-origin row. The evaluator never
    derives labels from target-time values, preventing future data from changing a
    historical segment assignment.
    """
    active_segments = tuple(segments or (Segment(*item) for item in DEFAULT_SEGMENTS))
    active_policy = policy or SegmentEvaluationPolicy()
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if active_policy.minimum_paired_samples < 1:
        raise ValueError("minimum_paired_samples must be positive")
    candidate, candidate_excluded = _eligible(candidate_rows, now.astimezone(timezone.utc))
    baseline, baseline_excluded = _eligible(baseline_rows, now.astimezone(timezone.utc))
    shared = sorted(set(candidate) & set(baseline))
    report_segments: dict[str, dict[str, Any]] = {}
    protected_failures: list[str] = []
    for segment in active_segments:
        pairs = [
            key
            for key in shared
            if _segment_value(candidate[key], segment.dimension) == segment.value
            and _segment_value(baseline[key], segment.dimension) == segment.value
        ]
        candidate_errors = [
            value
            for key in pairs
            if (value := _number(candidate[key]["absolute_error_pct"])) is not None
        ]
        baseline_errors = [
            value
            for key in pairs
            if (value := _number(baseline[key]["absolute_error_pct"])) is not None
        ]
        if len(candidate_errors) != len(pairs) or len(baseline_errors) != len(pairs):
            raise RuntimeError("eligible rows must have finite absolute errors")
        evidence = paired_bootstrap_comparison(
            candidate_errors,
            baseline_errors,
            metric="absolute_error_pct",
            lower_is_better=True,
            iterations=active_policy.bootstrap_iterations,
            min_samples=active_policy.minimum_paired_samples,
        )
        baseline_mean = evidence["baseline_mean"]
        improvement = evidence["relative_effect_size"]
        low_sample = len(pairs) < active_policy.minimum_paired_samples
        degradation = (
            improvement is not None
            and improvement < -active_policy.maximum_protected_relative_mae_degradation
        )
        blocked = segment.protected and not low_sample and degradation
        if blocked:
            protected_failures.append(segment.name)
        report_segments[segment.name] = {
            "dimension": segment.dimension,
            "value": segment.value,
            "protected": segment.protected,
            "paired_samples": len(pairs),
            "low_sample": low_sample,
            "low_sample_reason": f"paired_samples:{len(pairs)}<{active_policy.minimum_paired_samples}"
            if low_sample
            else None,
            "candidate_mean_absolute_error_pct": evidence["candidate_mean"],
            "baseline_mean_absolute_error_pct": baseline_mean,
            "relative_mae_improvement": improvement,
            "paired_evidence": evidence,
            "material_degradation": degradation,
            "promotion_blocked_without_approval": blocked,
        }
    return {
        "evaluation_version": EVALUATION_VERSION,
        "evaluated_at": now.astimezone(timezone.utc).isoformat(),
        "policy": asdict(active_policy),
        "policy_id": policy_identity(active_policy, active_segments),
        "pairing_key": ["origin_at", "horizon_hours"],
        "timestamp_safety": {
            "requires_timezone_aware_origin_and_target": True,
            "requires_target_after_origin": True,
            "uses_only_targets_matured_by_now": True,
            "segment_labels_are_origin_time_inputs": True,
        },
        "input": {
            "candidate_rows": len(candidate_rows),
            "baseline_rows": len(baseline_rows),
            "paired_rows": len(shared),
            "candidate_excluded": candidate_excluded,
            "baseline_excluded": baseline_excluded,
        },
        "segments": report_segments,
        "promotion_guard": {
            "protected_segment_material_degradation": protected_failures,
            "blocked_without_approval": bool(protected_failures),
        },
    }
