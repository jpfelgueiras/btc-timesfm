"""Distributional forecast evaluation metrics: pinball loss, interval score,
coverage diagnostics, and conditional coverage by horizon/regime."""

from __future__ import annotations

from typing import Any


def pinball_loss(actual: float, quantile_value: float, quantile_level: float) -> float:
    """Compute the pinball (quantile) loss for a single observation.

    Parameters
    ----------
    actual:
        Observed/actual value.
    quantile_value:
        Predicted quantile value (e.g. q10, q50, q90).
    quantile_level:
        Quantile level in (0, 1), e.g. 0.10 for q10, 0.90 for q90.

    Returns
    -------
    float
        Pinball loss: max(quantile_level * (actual - quantile_value),
        (quantile_level - 1) * (actual - quantile_value)).
    """
    error = actual - quantile_value
    if quantile_level < 0.5:
        return quantile_level * error if error > 0 else - (1.0 - quantile_level) * error
    else:
        return (quantile_level - 1.0) * error if error > 0 else quantile_level * error


def quantile_metrics(
    actual: float,
    *,
    q10: float | None = None,
    q50: float | None = None,
    q90: float | None = None,
) -> dict[str, float | None]:
    """Return pinball losses for available quantiles."""

    def _loss(q: float | None, level: float) -> float | None:
        if q is None:
            return None
        return pinball_loss(actual, q, level)

    return {
        "pinball_10": _loss(q10, 0.10),
        "pinball_50": _loss(q50, 0.50),
        "pinball_90": _loss(q90, 0.90),
    }


def interval_score(
    actual: float,
    lower: float,
    upper: float,
    alpha: float = 0.20,
) -> float:
    """Compute the Winkler interval score for a Q10–Q90 interval.

    Parameters
    ----------
    actual:
        Observed value.
    lower:
        Lower bound (e.g. q10).
    upper:
        Upper bound (e.g. q90).
    alpha:
        Nominal coverage level, default 0.20 (= 1 - 0.80 interval).

    Returns
    -------
    float
        Interval score: (upper - lower) / alpha
        + 2 / alpha * (lower - actual) if actual < lower
        + 2 / alpha * (actual - upper) if actual > upper
        + 0 if lower <= actual <= upper.
    """
    if actual < lower:
        return (upper - lower) / alpha + 2.0 / alpha * (lower - actual)
    if actual > upper:
        return (upper - lower) / alpha + 2.0 / alpha * (actual - upper)
    return (upper - lower) / alpha


def interval_width_pct(lower: float, upper: float, reference_price: float) -> float | None:
    """Return interval width as a percentage of the reference price."""
    if reference_price == 0 or lower is None or upper is None:
        return None
    return (upper - lower) / reference_price * 100.0


def coverage_error(
    observed_coverage: float,
    target_coverage: float = 0.80,
) -> float:
    """Absolute calibration error: |observed - target|."""
    return abs(observed_coverage - target_coverage)


def conditional_coverage_by_regime(
    rows: list[dict[str, Any]],
    *,
    horizon: int | None = None,
    regime_field: str = "regime",
) -> dict[str, dict[str, float | None]]:
    """Compute coverage diagnostics split by regime.

    Parameters
    ----------
    rows:
        Exported forecast history rows, each containing
        ``regime`` and ``within_q10_q90`` fields.
    horizon:
        If provided, only include rows with this horizon.
    regime_field:
        Dict key for the regime label.

    Returns
    -------
    dict[str, dict[str, float | None]]
        Keyed by regime name: ``{ "coverage": float, "samples": int }``.
    """
    from collections import defaultdict

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("actual_target_price_usd") is None:
            continue
        if horizon is not None and int(row.get("horizon_hours", 0)) != horizon:
            continue
        regime = str(row.get(regime_field) or "unknown")
        grouped[regime].append(row)

    result: dict[str, dict[str, float | None]] = {}
    for regime, regime_rows in sorted(grouped.items()):
        total = len(regime_rows)
        if total == 0:
            result[regime] = {"coverage": None, "samples": 0}
            continue
        interval_hits = sum(
            1
            for row in regime_rows
            if row.get("within_q10_q90") is not None
            and row["within_q10_q90"] == 1
        )
        coverage = interval_hits / total if total > 0 else None
        result[regime] = {"coverage": round(coverage, 4) if coverage is not None else None, "samples": total}
    return result