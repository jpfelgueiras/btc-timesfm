"""Conformal-style calibration for BTC forecast intervals.

Calibration only consumes matured forecasts that predate the forecast being
created. Production can use durable outcomes attached to history snapshots;
walk-forward callers can supply only target candles available at that origin.
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import Any

import numpy as np

DEFAULT_TARGET_COVERAGE = float(os.getenv("BTC_INTERVAL_TARGET_COVERAGE", "0.80"))
DEFAULT_HISTORY_LIMIT = int(os.getenv("BTC_CONFORMAL_HISTORY_LIMIT", "200"))
DEFAULT_MIN_SAMPLES = int(os.getenv("BTC_CONFORMAL_MIN_SAMPLES", "20"))
MIN_MULTIPLIER = 0.50
MAX_MULTIPLIER = 3.00


def _validate_config(target_coverage: float, history_limit: int, min_samples: int) -> None:
    if not 0.5 < target_coverage < 1.0:
        raise ValueError("target_coverage must be between 0.5 and 1.0")
    if history_limit < 1:
        raise ValueError("history_limit must be positive")
    if min_samples < 1:
        raise ValueError("min_samples must be positive")


def _origin(snapshot: dict[str, Any]) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(snapshot["latest_close_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _actual(
    snapshot: dict[str, Any],
    horizon: str,
    hour: int,
    actual_by_timestamp: dict[int, float],
) -> float | None:
    try:
        value = float(snapshot["_outcomes"][horizon]["ensemble"]["actual_target_price_usd"])
        if value > 0:
            return value
    except (KeyError, TypeError, ValueError):
        pass
    origin = _origin(snapshot)
    if origin is None:
        return None
    value = actual_by_timestamp.get(int(origin.timestamp()) + hour * 3600)
    if value is None:
        return None
    value = float(value)
    return value if value > 0 else None


def _sample(
    snapshot: dict[str, Any],
    actual_by_timestamp: dict[int, float],
    hour: int,
) -> dict[str, float] | None:
    horizon = f"{hour}h"
    try:
        pred = snapshot["predictions"][horizon]
        point = float(pred["price_usd"])
        q10 = float(pred["q10_usd"])
        q90 = float(pred["q90_usd"])
    except (KeyError, TypeError, ValueError):
        return None
    actual = _actual(snapshot, horizon, hour, actual_by_timestamp)
    if actual is None:
        return None
    half_width = (q90 - q10) / 2.0
    if point <= 0 or half_width <= 0:
        return None
    return {
        "actual": actual,
        "point": point,
        "half_width": half_width,
        "score": abs(actual - point) / half_width,
        "width_pct": (2.0 * half_width / point) * 100.0,
    }


def collect_scores(
    history: list[dict[str, Any]],
    actual_by_timestamp: dict[int, float],
    hour: int,
    *,
    regime: str | None = None,
    history_limit: int = DEFAULT_HISTORY_LIMIT,
) -> list[dict[str, float]]:
    """Return newest matured normalized-residual samples, optionally by regime."""
    samples: list[dict[str, float]] = []
    for snapshot in reversed(history):
        if regime is not None and snapshot.get("regime") != regime:
            continue
        sample = _sample(snapshot, actual_by_timestamp, hour)
        if sample is None:
            continue
        samples.append(sample)
        if len(samples) >= history_limit:
            break
    samples.reverse()
    return samples


def _legacy_multiplier(
    samples: list[dict[str, float]], target_coverage: float
) -> tuple[float, float | None]:
    if len(samples) < 10:
        return 1.0, None
    coverage = float(np.mean([sample["score"] <= 1.0 for sample in samples]))
    multiplier = math.sqrt(target_coverage / max(coverage, 0.10))
    return float(np.clip(multiplier, 0.75, 1.75)), coverage


def _conformal_multiplier(samples: list[dict[str, float]], target_coverage: float) -> float:
    n = len(samples)
    level = min(1.0, math.ceil((n + 1) * target_coverage) / n)
    values = np.asarray([sample["score"] for sample in samples], dtype=float)
    multiplier = float(np.quantile(values, level, method="higher"))
    return float(np.clip(multiplier, MIN_MULTIPLIER, MAX_MULTIPLIER))


def calibration_details(
    history: list[dict[str, Any]],
    actual_by_timestamp: dict[int, float],
    hour: int,
    *,
    regime: str | None = None,
    target_coverage: float = DEFAULT_TARGET_COVERAGE,
    history_limit: int = DEFAULT_HISTORY_LIMIT,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> dict[str, Any]:
    """Return no-lookahead calibration parameters and comparison diagnostics."""
    _validate_config(target_coverage, history_limit, min_samples)
    all_samples = collect_scores(
        history, actual_by_timestamp, hour, history_limit=history_limit
    )
    regime_samples = (
        collect_scores(
            history,
            actual_by_timestamp,
            hour,
            regime=regime,
            history_limit=history_limit,
        )
        if regime is not None
        else []
    )
    if len(regime_samples) >= min_samples:
        selected = regime_samples
        source = "regime"
    else:
        selected = all_samples
        source = "all_regimes"

    legacy_multiplier, raw_coverage = _legacy_multiplier(selected, target_coverage)
    if len(selected) < min_samples:
        multiplier = legacy_multiplier
        mode = "legacy_fallback"
    else:
        multiplier = _conformal_multiplier(selected, target_coverage)
        mode = "conformal"

    calibrated_coverage = (
        float(np.mean([sample["score"] <= multiplier for sample in selected]))
        if selected
        else None
    )
    avg_width = (
        float(np.mean([sample["width_pct"] for sample in selected])) if selected else None
    )
    return {
        "mode": mode,
        "source": source,
        "horizon": f"{hour}h",
        "target_coverage": target_coverage,
        "samples": len(selected),
        "regime_samples": len(regime_samples),
        "all_regime_samples": len(all_samples),
        "history_limit": history_limit,
        "min_samples": min_samples,
        "multiplier": multiplier,
        "legacy_multiplier": legacy_multiplier,
        "empirical_coverage_before": raw_coverage,
        "empirical_coverage_after": calibrated_coverage,
        "average_interval_width_pct_before": avg_width,
        "average_interval_width_pct_after": (
            avg_width * multiplier if avg_width is not None else None
        ),
        "legacy_average_interval_width_pct": (
            avg_width * legacy_multiplier if avg_width is not None else None
        ),
    }


def conformal_calibration_multiplier(
    history: list[dict[str, Any]],
    actual_by_timestamp: dict[int, float],
    hour: int,
    target_coverage: float = DEFAULT_TARGET_COVERAGE,
) -> tuple[float, int, float | None]:
    """Compatibility adapter for forecast_engine's interval-calibration hook."""
    details = calibration_details(
        history,
        actual_by_timestamp,
        hour,
        target_coverage=target_coverage,
    )
    return (
        float(details["multiplier"]),
        int(details["samples"]),
        details["empirical_coverage_after"],
    )


def evaluation_report(
    history: list[dict[str, Any]],
    actual_by_timestamp: dict[int, float],
    *,
    regime: str | None = None,
    target_coverage: float = DEFAULT_TARGET_COVERAGE,
) -> dict[str, Any]:
    """Compare conformal and legacy interval behavior for every production horizon."""
    return {
        f"{hour}h": calibration_details(
            history,
            actual_by_timestamp,
            hour,
            regime=regime,
            target_coverage=target_coverage,
        )
        for hour in (2, 4, 8, 16)
    }
