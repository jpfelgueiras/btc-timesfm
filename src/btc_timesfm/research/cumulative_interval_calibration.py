"""Leakage-safe evaluation helpers for cumulative interval experiments.

Per-step marginal quantiles are never treated as cumulative quantiles.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import re
from typing import Any


def _time(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return parsed


def matured_residuals(
    rows: list[dict[str, Any]], *, origin: str | datetime, horizon: int
) -> list[float]:
    """Return verified raw cumulative residuals whose exact target is mature by origin."""
    cutoff = _time(origin)
    selected = []
    for row in rows:
        if row.get("stage") != "raw_cumulative" or not row.get("raw_lineage_verified"):
            continue
        if int(row.get("horizon", -1)) != horizon or row.get("actual") is None:
            continue
        if _time(row["target_at"]) <= cutoff:
            selected.append(float(row["actual"]) - float(row["q50"]))
    return selected


def interval_metrics(rows: list[dict[str, Any]]) -> dict[str, float | int | None]:
    """Score supplied cumulative q10/q50/q90 intervals."""
    usable = [row for row in rows if row.get("actual") is not None]
    if not usable:
        return {
            "n": 0,
            "pinball_q10": None,
            "pinball_q50": None,
            "pinball_q90": None,
            "coverage_80": None,
            "mean_width": None,
            "normalized_width": None,
            "interval_score_80": None,
            "weighted_interval_score": None,
            "mean_absolute_error": None,
        }
    scores: dict[str, float] = {}
    for name, alpha in (("q10", 0.1), ("q50", 0.5), ("q90", 0.9)):
        scores[f"pinball_{name}"] = sum(
            max(
                alpha * (float(row["actual"]) - float(row[name])),
                (alpha - 1) * (float(row["actual"]) - float(row[name])),
            )
            for row in usable
        ) / len(usable)
    widths = [float(row["q90"]) - float(row["q10"]) for row in usable]
    if any(width < 0 for width in widths):
        raise ValueError("interval bounds must satisfy q10 <= q90")
    covered = [float(row["q10"]) <= float(row["actual"]) <= float(row["q90"]) for row in usable]
    interval = [
        width
        + 10 * max(float(row["q10"]) - float(row["actual"]), 0)
        + 10 * max(float(row["actual"]) - float(row["q90"]), 0)
        for row, width in zip(usable, widths)
    ]
    mean_width = sum(widths) / len(widths)
    scale = sum(abs(float(row["actual"])) for row in usable) / len(usable)
    mae = sum(abs(float(row["actual"]) - float(row["q50"])) for row in usable) / len(usable)
    interval_score = sum(interval) / len(interval)
    return {
        "n": len(usable),
        **scores,
        "coverage_80": sum(covered) / len(usable),
        "mean_width": mean_width,
        "normalized_width": mean_width / scale if scale else None,
        "interval_score_80": interval_score,
        "weighted_interval_score": (0.5 * mae + 0.1 * interval_score) / 1.5,
        "mean_absolute_error": mae,
    }


def build_gate_report(
    *,
    raw_predictions: list[dict[str, Any]],
    matured_outcomes: list[dict[str, Any]],
    canonical_audit: dict[str, Any] | None = None,
    target_pipeline_ready: bool = False,
) -> dict[str, Any]:
    """Fail closed unless canonical data, complete linked lineage, and pair support exist."""
    reasons = []
    audit = canonical_audit or {}
    digest = audit.get("manifest_sha256")
    if (
        audit.get("status") != "ready"
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or audit.get("exact_target_coverage") is not True
        or audit.get("prospective_period_ready") is not True
    ):
        reasons.append(
            "#339 canonical audit manifest, exact target coverage, or prospective period is absent"
        )
    try:
        frozen_at = _time(audit["frozen_at"])
        prospective_start = _time(audit["prospective_start"])
        evaluated_at = _time(audit["evaluated_at"])
        if prospective_start < frozen_at:
            raise ValueError("prospective start precedes freeze")
        if evaluated_at < prospective_start + timedelta(days=30):
            raise ValueError("prospective observation period is shorter than 30 days")
    except (KeyError, TypeError, ValueError):
        frozen_at = prospective_start = evaluated_at = None
        reasons.append("canonical temporal manifest is missing, invalid, or shorter than 30 days")
    if not target_pipeline_ready:
        reasons.append("#343 target pipeline gate is not ready")

    required_stages = {"raw_cumulative", "pre_coherence", "final"}
    indexed: dict[tuple[str, int, str], dict[str, dict[str, Any]]] = {}
    invalid_lineage = False
    for row in raw_predictions:
        try:
            key = (
                _time(row["origin_at"]).isoformat(),
                int(row["horizon"]),
                _time(row["target_at"]).isoformat(),
            )
            stage = row["stage"]
            if (
                stage not in required_stages
                or not row.get("source_identity")
                or not row.get("candidate_identity")
            ):
                invalid_lineage = True
                continue
            stages = indexed.setdefault(key, {})
            if stage in stages:
                invalid_lineage = True
            stages[stage] = row
        except (KeyError, TypeError, ValueError):
            invalid_lineage = True

    complete: dict[tuple[str, int, str], dict[str, dict[str, Any]]] = {}
    for key, stages in indexed.items():
        if set(stages) != required_stages:
            invalid_lineage = True
            continue
        identities = {
            (row["source_identity"], row["candidate_identity"]) for row in stages.values()
        }
        if len(identities) != 1:
            invalid_lineage = True
            continue
        complete[key] = stages
    if invalid_lineage or not complete:
        reasons.append(
            "complete unique raw/pre-coherence/final linked lineage is absent or inconsistent"
        )

    outcomes: dict[tuple[str, int, str], dict[str, Any]] = {}
    invalid_outcomes = False
    for outcome in matured_outcomes:
        try:
            key = (
                _time(outcome["origin_at"]).isoformat(),
                int(outcome["horizon"]),
                _time(outcome["target_at"]).isoformat(),
            )
            if outcome.get("actual") is None or key in outcomes:
                invalid_outcomes = True
            else:
                target_at = _time(outcome["target_at"])
                actual_at = _time(outcome["actual_at"])
                matured_at = _time(outcome["matured_at"])
                if (
                    actual_at != target_at
                    or matured_at < target_at
                    or evaluated_at is None
                    or evaluated_at <= target_at
                    or evaluated_at <= matured_at
                ):
                    invalid_outcomes = True
                    continue
                outcomes[key] = outcome
        except (KeyError, TypeError, ValueError):
            invalid_outcomes = True
    paired = complete.keys() & outcomes.keys()
    if outcomes.keys() - complete.keys():
        invalid_outcomes = True
    if invalid_outcomes or not paired:
        reasons.append("unique exact-target matured outcomes are not paired to complete lineage")

    counts: dict[int, int] = {}
    prospective_counts: dict[int, int] = {}
    prospective_origins: dict[int, list[datetime]] = {}
    for key in paired:
        horizon = key[1]
        origin_at = _time(key[0])
        if frozen_at is not None and origin_at < frozen_at:
            counts[horizon] = counts.get(horizon, 0) + 1
        elif (
            prospective_start is not None
            and evaluated_at is not None
            and prospective_start <= origin_at < evaluated_at
        ):
            prospective_counts[horizon] = prospective_counts.get(horizon, 0) + 1
            prospective_origins.setdefault(horizon, []).append(origin_at)
        else:
            invalid_outcomes = True
    if invalid_outcomes:
        reasons.append(
            "outcomes contain invalid joins, maturity timestamps, or out-of-window origins"
        )
    required_horizons = set(audit.get("required_horizons", (2, 4, 8, 16)))
    prospective_windows: dict[int, dict[str, Any]] = {}
    for horizon in required_horizons:
        origins = prospective_origins.get(horizon, [])
        first = min(origins) if origins else None
        last = max(origins) if origins else None
        span = (last - first) if first is not None and last is not None else None
        prospective_windows[horizon] = {
            "start_at": first.isoformat() if first is not None else None,
            "end_at": last.isoformat() if last is not None else None,
            "span_days": span.total_seconds() / 86400 if span is not None else None,
        }
        if (
            prospective_start is None
            or first is None
            or last is None
            or abs(first - prospective_start) > timedelta(hours=1)
            or span is None
            or span < timedelta(days=30)
        ):
            reasons.append(
                f"prospective paired origins for horizon {horizon} do not span the required 30 days"
            )
    if any(
        counts.get(horizon, 0) < 200 or prospective_counts.get(horizon, 0) < 200
        for horizon in required_horizons
    ):
        reasons.append(
            "fewer than 200 exact pairs per horizon in both evaluation and prospective periods"
        )
    return {
        "status": "blocked" if reasons else "ready_for_evaluation",
        "blocked_reasons": reasons,
        "calibration_skill_claims": False,
        "per_step_marginals_as_cumulative_quantiles": False,
        "stages": ["raw", "pre_coherence", "final"],
        "paired_counts_by_horizon": counts,
        "prospective_paired_counts_by_horizon": prospective_counts,
        "prospective_origin_windows_by_horizon": prospective_windows,
        "metrics": [
            "pinball_q10_q50_q90",
            "coverage_80",
            "interval_score_80",
            "weighted_interval_score",
            "normalized_width",
            "conditional_calibration",
            "point_mae_non_regression",
        ],
    }
