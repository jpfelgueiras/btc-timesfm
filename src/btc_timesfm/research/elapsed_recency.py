"""Leakage-safe recency weights for bounded regime-policy experiments."""

from __future__ import annotations

import math

SECONDS_PER_HOUR = 3600.0


def elapsed_time_weights(
    outcome_timestamps: list[int], *, available_at: int, half_life_hours: float
) -> list[float]:
    """Return exponential weights for matured outcomes as of a forecast cutoff.

    Future outcomes are rejected rather than silently included. Decay depends
    on elapsed wall-clock time, so irregular origin cadence does not change the
    meaning of a row-count window.
    """
    if not math.isfinite(half_life_hours) or half_life_hours <= 0.0:
        raise ValueError("half_life_hours must be finite and positive")
    if any(timestamp > available_at for timestamp in outcome_timestamps):
        raise ValueError("outcomes after available_at must not affect the forecast")
    decay = math.log(2.0) / (half_life_hours * SECONDS_PER_HOUR)
    return [math.exp(-decay * (available_at - timestamp)) for timestamp in outcome_timestamps]


def effective_sample_size(weights: list[float]) -> float:
    """Compute Kish effective sample size for nonnegative evidence weights."""
    if any(not math.isfinite(weight) or weight < 0.0 for weight in weights):
        raise ValueError("weights must be finite and nonnegative")
    total = sum(weights)
    squared = sum(weight * weight for weight in weights)
    if total == 0.0 or squared == 0.0:
        return 0.0
    return total * total / squared
