"""Rolling recalibration of forecast uncertainty."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from btc_timesfm.forecasting.conditional_calibration import (
    DEFAULT_COVERAGE_TOLERANCE,
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_MIN_SAMPLES,
    DEFAULT_TARGET_COVERAGE,
    DEFAULT_VOL_BUCKET_CUTS,
    bucket_calibration_details,
)

# Maximum relative change to a multiplier in a single recalibration step
MAX_MULTIPLIER_ADJUSTMENT = 0.10


def _validate_adjustment(old: float, new: float) -> float:
    """Enforce guardrail: clamp adjustment to MAX_MULTIPLIER_ADJUSTMENT."""
    max_delta = old * MAX_MULTIPLIER_ADJUSTMENT
    return float(np.clip(new, old - max_delta, old + max_delta))


def recalibrate(
    history: list[dict[str, Any]],
    actual_by_timestamp: dict[int, float],
    current_multipliers: dict[str, float],
    *,
    regime: str | None = None,
    market_features: dict[str, Any] | None = None,
    target_coverage: float = DEFAULT_TARGET_COVERAGE,
    history_limit: int = DEFAULT_HISTORY_LIMIT,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    tolerance: float = DEFAULT_COVERAGE_TOLERANCE,
    cuts: tuple[float, float] = DEFAULT_VOL_BUCKET_CUTS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Check for drift and compute recalibrated multipliers with guardrails."""
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    report: dict[str, Any] = {
        "generated_at": current_time.isoformat(),
        "horizons": {},
        "action": "none",
        "changes": {},
    }

    needs_recalibration = False
    new_multipliers = {}

    for hour in (2, 4, 8, 16):
        details = bucket_calibration_details(
            history,
            actual_by_timestamp,
            hour,
            regime=regime,
            market_features=market_features,
            target_coverage=target_coverage,
            history_limit=history_limit,
            min_samples=min_samples,
            tolerance=tolerance,
            cuts=cuts,
            now=now,
        )

        bucket_name = details["selected_bucket"]
        bucket_entry = details["buckets"][bucket_name]

        current_mult = current_multipliers.get(f"{hour}h", 1.0)
        recommended_mult = float(bucket_entry["multiplier"])

        # Check for breach
        is_breach = (
            not bool(bucket_entry["within_tolerance"])
            and int(bucket_entry["samples"]) >= min_samples
        )

        report["horizons"][f"{hour}h"] = {
            "bucket": bucket_name,
            "current_multiplier": current_mult,
            "recommended_multiplier": recommended_mult,
            "is_breach": is_breach,
        }

        if is_breach:
            needs_recalibration = True
            new_mult = _validate_adjustment(current_mult, recommended_mult)
            new_multipliers[f"{hour}h"] = new_mult
            report["changes"][f"{hour}h"] = {
                "before": current_mult,
                "after": new_mult,
                "justification": "coverage_breach",
            }

    if needs_recalibration:
        report["action"] = "recalibrate"
        report["new_multipliers"] = {**current_multipliers, **new_multipliers}

    return report
