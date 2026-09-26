"""Leakage-safe, bounded evaluation helpers for cumulative interval experiments.

Inputs are cumulative-horizon outcomes and explicitly identified raw/final stages;
per-step marginal quantiles are never treated as cumulative quantiles here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def _time(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return parsed


def matured_residuals(
    rows: list[dict[str, Any]], *, origin: str | datetime, horizon: int
) -> list[float]:
    """Return raw cumulative residuals known by the forecast origin, exact target time included."""
    cutoff = _time(origin)
    selected = []
    for row in rows:
        if row.get("stage") != "raw_cumulative" or not row.get("raw_lineage_verified"):
            continue
        if int(row.get("horizon", -1)) != horizon or row.get("actual") is None:
            continue
        target = _time(row["target_at"])
        if target <= cutoff:
            selected.append(float(row["actual"]) - float(row["q50"]))
    return selected


def interval_metrics(rows: list[dict[str, Any]]) -> dict[str, float | int | None]:
    """Score supplied cumulative q10/q50/q90 intervals; no marginal accumulation is inferred."""
    usable = [r for r in rows if r.get("actual") is not None]
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
                alpha * (float(r["actual"]) - float(r[name])),
                (alpha - 1) * (float(r["actual"]) - float(r[name])),
            )
            for r in usable
        ) / len(usable)
    widths = [float(r["q90"]) - float(r["q10"]) for r in usable]
    if any(width < 0 for width in widths):
        raise ValueError("interval bounds must satisfy q10 <= q90")
    covered = [float(r["q10"]) <= float(r["actual"]) <= float(r["q90"]) for r in usable]
    interval = [
        width
        + 10 * max(float(r["q10"]) - float(r["actual"]), 0)
        + 10 * max(float(r["actual"]) - float(r["q90"]), 0)
        for r, width in zip(usable, widths)
    ]
    scale = sum(abs(float(r["actual"])) for r in usable) / len(usable)
    return {
        "n": len(usable),
        **scores,
        "coverage_80": sum(covered) / len(usable),
        "mean_width": sum(widths) / len(widths),
        "normalized_width": sum(widths) / len(widths) / scale if scale else None,
        "interval_score_80": sum(interval) / len(interval),
        "weighted_interval_score": (
            scores["pinball_q10"]
            + 2 * scores["pinball_q50"]
            + scores["pinball_q90"]
            + 0.1 * sum(widths) / len(widths)
        )
        / 4,
        "mean_absolute_error": sum(abs(float(r["actual"]) - float(r["q50"])) for r in usable)
        / len(usable),
    }


def build_gate_report(
    *,
    raw_predictions: list[dict[str, Any]],
    matured_outcomes: list[dict[str, Any]],
    dataset_ready: bool,
    target_pipeline_ready: bool,
) -> dict[str, Any]:
    """Return an explicit blocked/ready report without making calibration skill claims."""
    reasons = []
    if not raw_predictions or not any(r.get("raw_lineage_verified") for r in raw_predictions):
        reasons.append("immutable raw per-origin paired prediction lineage is absent")
    if not matured_outcomes:
        reasons.append("exact-target-time matured outcomes are absent")
    if not dataset_ready:
        reasons.append("#339 canonical BTC/USD dataset gate is not ready")
    if not target_pipeline_ready:
        reasons.append("#343 target pipeline gate is not ready")
    return {
        "status": "blocked" if reasons else "ready_for_evaluation",
        "blocked_reasons": reasons,
        "calibration_skill_claims": False,
        "per_step_marginals_as_cumulative_quantiles": False,
        "stages": ["raw", "pre_coherence", "final"],
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
