"""Safe rolling recalibration of forecast intervals from matured history only."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from btc_timesfm.forecasting.conditional_calibration import (
    DEFAULT_COVERAGE_TOLERANCE,
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_MIN_SAMPLES,
    DEFAULT_TARGET_COVERAGE,
    DEFAULT_VOL_BUCKET_CUTS,
    _origin_time,
    _sample,
    bucket_calibration_details,
)

HORIZONS = (2, 4, 8, 16)
MAX_MULTIPLIER_ADJUSTMENT = 0.10
MAX_UNSAFE_MULTIPLIER_ADJUSTMENT = 0.50
DEFAULT_DIRECTIONAL_ACCURACY_FLOOR = 0.50
DEFAULT_DIRECTIONAL_TOLERANCE = 0.05


def _matured(snapshot: dict[str, Any], hour: int, now: datetime) -> bool:
    origin = _origin_time(snapshot)
    return origin is not None and origin + timedelta(hours=hour) <= now


def _validate_adjustment(old: float, new: float) -> float:
    """Compatibility helper that clamps one safe recalibration step."""
    delta = old * MAX_MULTIPLIER_ADJUSTMENT
    return max(old - delta, min(new, old + delta))


def _bounded_adjustment(old: float, recommended: float) -> tuple[float | None, str | None]:
    if not all(math.isfinite(value) and value > 0.0 for value in (old, recommended)):
        return None, "invalid_multiplier"
    relative_change = abs(recommended - old) / old
    if relative_change > MAX_UNSAFE_MULTIPLIER_ADJUSTMENT:
        return None, "unsafe_multiplier_change"
    return _validate_adjustment(old, recommended), None


def _directional_accuracy(
    history: list[dict[str, Any]], actual_by_timestamp: dict[int, float], hour: int, now: datetime
) -> tuple[int, float | None]:
    correct: list[bool] = []
    for snapshot in history:
        if not _matured(snapshot, hour, now):
            continue
        sample = _sample(snapshot, actual_by_timestamp, hour)
        if sample is None:
            continue
        try:
            origin_price = float(snapshot["latest_close_usd"])
        except (KeyError, TypeError, ValueError):
            continue
        if origin_price <= 0.0:
            continue
        predicted_move = float(sample["point"]) - origin_price
        actual_move = float(sample["actual"]) - origin_price
        correct.append((predicted_move >= 0.0) == (actual_move >= 0.0))
    return len(correct), (sum(correct) / len(correct) if correct else None)


def append_audit_entry(
    audit_history: list[dict[str, Any]], report: Mapping[str, Any]
) -> dict[str, Any]:
    """Append a compact immutable decision record and return it."""
    entry = {
        "timestamp": report["generated_at"],
        "action": report["action"],
        "changes": report["changes"],
        "alerts": report["alerts"],
    }
    audit_history.append(entry)
    return entry


def rollback_last_recalibration(
    current_multipliers: Mapping[str, float],
    audit_history: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Restore values from the latest applied recalibration and audit the rollback."""
    for entry in reversed(audit_history):
        if entry.get("action") != "recalibrate":
            continue
        restored = dict(current_multipliers)
        for horizon, change in entry.get("changes", {}).items():
            if isinstance(change, Mapping) and change.get("status") == "applied":
                restored[horizon] = float(change["before"])
        timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        result = {
            "generated_at": timestamp,
            "action": "rollback",
            "new_multipliers": restored,
            "rolled_back_at": entry["timestamp"],
        }
        audit_history.append(
            {
                "timestamp": timestamp,
                "action": "rollback",
                "changes": {},
                "alerts": [],
                "rolled_back_at": entry["timestamp"],
            }
        )
        return result
    raise ValueError("no applied recalibration is available to roll back")


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
    directional_accuracy_floor: float = DEFAULT_DIRECTIONAL_ACCURACY_FLOOR,
    directional_tolerance: float = DEFAULT_DIRECTIONAL_TOLERANCE,
    now: datetime | None = None,
    audit_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Detect coverage and directional drift and safely propose interval changes."""
    if not 0.0 <= directional_accuracy_floor <= 1.0 or directional_tolerance < 0.0:
        raise ValueError("invalid directional calibration thresholds")
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    report: dict[str, Any] = {
        "generated_at": current_time.isoformat(),
        "horizons": {},
        "action": "none",
        "changes": {},
        "alerts": [],
        "new_multipliers": dict(current_multipliers),
    }

    applied = False
    for hour in HORIZONS:
        key = f"{hour}h"
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
            now=current_time,
        )
        bucket = details["buckets"][details["selected_bucket"]]
        samples, direction_accuracy = _directional_accuracy(
            history, actual_by_timestamp, hour, current_time
        )
        coverage_breach = (
            not bool(bucket["within_tolerance"]) and int(bucket["samples"]) >= min_samples
        )
        directional_breach = (
            samples >= min_samples
            and direction_accuracy is not None
            and direction_accuracy < directional_accuracy_floor - directional_tolerance
        )
        entry = {
            "bucket": details["selected_bucket"],
            "matured_samples": int(bucket["samples"]),
            "coverage": bucket["coverage_after"],
            "coverage_breach": coverage_breach,
            "directional_samples": samples,
            "directional_accuracy": direction_accuracy,
            "directional_breach": directional_breach,
            "current_multiplier": current_multipliers.get(key, 1.0),
            "recommended_multiplier": float(bucket["multiplier"]),
        }
        report["horizons"][key] = entry
        if directional_breach:
            report["alerts"].append({"horizon": key, "reason": "directional_calibration_drift"})
        if not coverage_breach:
            continue
        before = float(current_multipliers.get(key, 1.0))
        after, rejection = _bounded_adjustment(before, float(bucket["multiplier"]))
        if rejection is not None:
            report["changes"][key] = {
                "before": before,
                "recommended": float(bucket["multiplier"]),
                "status": "rejected",
                "reason": rejection,
            }
            report["alerts"].append({"horizon": key, "reason": rejection})
            continue
        assert after is not None
        report["changes"][key] = {
            "before": before,
            "after": after,
            "recommended": float(bucket["multiplier"]),
            "status": "applied",
            "reason": "coverage_breach",
            "bounded": after != float(bucket["multiplier"]),
        }
        report["new_multipliers"][key] = after
        applied = True
    if applied:
        report["action"] = "recalibrate"
    elif report["alerts"]:
        report["action"] = "alert"
    if audit_history is not None:
        report["audit_entry"] = append_audit_entry(audit_history, report)
    return report


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run safe rolling interval recalibration")
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="JSON with history, actual_by_timestamp, multipliers",
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    payload = _load_json(args.input)
    audit = _load_json(args.audit) if args.audit.exists() else []
    if not isinstance(payload, dict) or not isinstance(audit, list):
        raise ValueError("input must be an object and audit must be an array")
    multipliers = payload.get("current_multipliers", {})
    if args.rollback:
        report = rollback_last_recalibration(multipliers, audit)
    else:
        report = recalibrate(
            payload.get("history", []),
            {
                int(key): float(value)
                for key, value in payload.get("actual_by_timestamp", {}).items()
            },
            multipliers,
            audit_history=audit,
        )
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.audit.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
