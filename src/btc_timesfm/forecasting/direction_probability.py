"""Calibrated probability-of-direction forecasts for the BTC ensemble.

Direction probabilities are estimated strictly from matured durable-history rows
whose ``target_at`` time has already passed, so no outcome observed after the
forecast-time cutoff can influence the estimate. Each horizon's probability is
the smoothed historical frequency of up/down/meaningful-move outcomes,
conditioned on the cohort of past forecasts whose predicted direction equals the
current forecast's predicted direction. Sparse cohorts shrink toward the
neutral 0.5 prior instead of emitting an overconfident probability.

The Brier score and reliability table computed from a supplied history are
retrospective leave-one-out diagnostics, not scores of probabilities actually
issued at each historical origin. They must not be interpreted as prequential
OOS calibration evidence. Issued probabilities need to be persisted with their
forecast/policy identity and scored unchanged only after maturity.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Iterable

from btc_timesfm.history.history_store import ENSEMBLE_MODEL

HORIZONS = ("2h", "4h", "8h", "16h")
DEFAULT_HORIZON_HOURS = (2, 4, 8, 16)
DIRECTION_PROBABILITY_VERSION = 1
MIN_EVIDENCE_SAMPLES = 20
SHRINKAGE_STRENGTH = 1.0
NEUTRAL_THRESHOLD_PCT = 0.25
DEFAULT_MOVE_THRESHOLD_PCT = 0.25
EPSILON = 1e-9
_RELIABILITY_BIN_EDGES = tuple(i / 10.0 for i in range(11))


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None else None


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return sum(items) / len(items) if items else None


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _horizon_hours(horizon: int | str) -> int:
    value = str(horizon)
    if value.endswith("h"):
        try:
            hour = int(value[:-1])
        except ValueError as exc:
            raise ValueError(f"invalid horizon: {horizon}") from exc
    else:
        try:
            hour = int(value)
        except ValueError as exc:
            raise ValueError(f"invalid horizon: {horizon}") from exc
    if hour <= 0:
        raise ValueError("horizon_hours must be positive")
    return hour


def classify_direction(
    change_pct: float,
    neutral_threshold_pct: float = NEUTRAL_THRESHOLD_PCT,
) -> str:
    """Classify a percentage move as up/down/neutral using a symmetric threshold."""
    if neutral_threshold_pct < 0:
        raise ValueError("neutral_threshold_pct must be >= 0")
    if change_pct > neutral_threshold_pct:
        return "up"
    if change_pct < -neutral_threshold_pct:
        return "down"
    return "neutral"


def _smoothed_rate(
    hits: int,
    total: int,
    strength: float = SHRINKAGE_STRENGTH,
    prior: float = 0.5,
) -> float:
    """Beta-posterior rate that shrinks toward ``prior`` when samples are sparse."""
    if total < 0 or not 0 <= hits <= total:
        raise ValueError("hits must be between 0 and total")
    if strength <= 0:
        raise ValueError("shrinkage_strength must be positive")
    return (hits + strength * prior) / (total + strength)


def _select_rows(
    rows: list[dict[str, Any]],
    *,
    now: datetime,
    model_name: str,
    horizon_hours: int,
) -> list[tuple[float, float]]:
    """Return (predicted_change_pct, actual_change_pct) pairs available at ``now``.

    A row counts only if it belongs to the model/horizon, is matured, and its
    outcome was already realized at the forecast-time cutoff (``target_at <= now``).
    """
    selected: list[tuple[float, float]] = []
    for row in rows:
        if str(row.get("model_name") or "unknown") != model_name:
            continue
        predicted = _finite_float(row.get("predicted_change_pct"))
        actual = _finite_float(row.get("actual_change_pct"))
        if predicted is None or actual is None:
            continue
        horizon_value = row.get("horizon_hours")
        if horizon_value is None:
            continue
        try:
            hour = int(horizon_value)
        except (TypeError, ValueError):
            continue
        if hour != horizon_hours:
            continue
        target = _parse_utc(row.get("target_at"))
        if target is None or target > now:
            continue
        selected.append((predicted, actual))
    return selected


def _leave_one_out_up(rows: list[tuple[float, float]]) -> list[tuple[float, int]]:
    """Per-row leave-one-out P(up) with the row removed from its own cohort."""
    total = len(rows)
    total_up = sum(1 for _, actual in rows if actual > EPSILON)
    estimates: list[tuple[float, int]] = []
    for _, actual in rows:
        outcome = 1 if actual > EPSILON else 0
        p_value = _smoothed_rate(total_up - outcome, total - 1)
        estimates.append((p_value, outcome))
    return estimates


def _brier_score(rows: list[tuple[float, int]]) -> float | None:
    if not rows:
        return None
    return float(sum((p_value - outcome) ** 2 for p_value, outcome in rows) / len(rows))


def _reliability_table(rows: list[tuple[float, int]]) -> dict[str, Any]:
    """Bucket leave-one-out probabilities and compare with observed frequency."""
    edges = _RELIABILITY_BIN_EDGES
    bins: list[dict[str, Any]] = []
    occupied = 0
    max_deviation: float | None = None
    for index in range(len(edges) - 1):
        low, high = edges[index], edges[index + 1]
        in_bin = [
            (p_value, outcome)
            for p_value, outcome in rows
            if low <= p_value < high or (index == len(edges) - 2 and p_value == 1.0)
        ]
        count = len(in_bin)
        predicted = _mean(p_value for p_value, _ in in_bin)
        observed = _mean(float(outcome) for _, outcome in in_bin) if in_bin else None
        bins.append(
            {
                "bin": f"[{low:.1f}, {high:.1f})",
                "count": count,
                "predicted_frequency": _round(predicted),
                "observed_frequency": _round(observed),
            }
        )
        if count and predicted is not None and observed is not None:
            occupied += 1
            deviation = abs(predicted - observed)
            max_deviation = deviation if max_deviation is None else max(max_deviation, deviation)

    total = len(rows)
    ece: float | None = None
    if total:
        weighted = []
        for binned in bins:
            count = int(binned["count"])
            if not count:
                continue
            predicted = binned["predicted_frequency"]
            observed = binned["observed_frequency"]
            if predicted is None or observed is None:
                continue
            weighted.append((count / total) * abs(predicted - observed))
        ece = float(sum(weighted)) if weighted else 0.0
    return {
        "bins": bins,
        "calibration_error": _round(ece),
        "max_bin_deviation": _round(max_deviation),
        "occupied_bins": occupied,
    }


def horizon_probabilities(
    rows: list[dict[str, Any]],
    *,
    now: datetime,
    model_name: str = ENSEMBLE_MODEL,
    horizon: int | str,
    predicted_change_pct: float | None = None,
    move_threshold_pct: float = DEFAULT_MOVE_THRESHOLD_PCT,
    neutral_threshold_pct: float = NEUTRAL_THRESHOLD_PCT,
    min_evidence_samples: int = MIN_EVIDENCE_SAMPLES,
    shrinkage_strength: float = SHRINKAGE_STRENGTH,
) -> dict[str, Any]:
    """Build calibrated directional probabilities for one model+horizon.

    ``predicted_change_pct`` is the ensemble change actually being forecast now;
    when supplied, the empirical cohort is restricted to matured forecasts whose
    predicted direction matches the current one. When it is ``None`` the marginal
    matured frequency over the whole horizon is used instead.
    """
    if move_threshold_pct < 0:
        raise ValueError("move_threshold_pct must be >= 0")
    if neutral_threshold_pct < 0:
        raise ValueError("neutral_threshold_pct must be >= 0")
    if min_evidence_samples < 1:
        raise ValueError("min_evidence_samples must be >= 1")
    if shrinkage_strength <= 0:
        raise ValueError("shrinkage_strength must be positive")

    hour = _horizon_hours(horizon)
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    selected = _select_rows(
        rows,
        now=current_time,
        model_name=model_name,
        horizon_hours=hour,
    )
    all_samples = len(selected)

    current_direction: str | None = None
    cohort = selected
    if predicted_change_pct is not None:
        current_direction = classify_direction(predicted_change_pct, neutral_threshold_pct)
        cohort = [
            (predicted, actual)
            for predicted, actual in selected
            if classify_direction(predicted, neutral_threshold_pct) == current_direction
        ]

    samples = len(cohort)
    n_up = sum(1 for _, actual in cohort if actual > EPSILON)
    n_down = sum(1 for _, actual in cohort if actual < -EPSILON)
    n_moved = sum(1 for _, actual in cohort if abs(actual) > move_threshold_pct)
    p_up = _smoothed_rate(n_up, samples, shrinkage_strength)
    p_down = _smoothed_rate(n_down, samples, shrinkage_strength)
    p_move = _smoothed_rate(n_moved, samples, shrinkage_strength)
    direction_accuracy = _mean(
        float(
            classify_direction(predicted, neutral_threshold_pct)
            == classify_direction(actual, neutral_threshold_pct)
        )
        for predicted, actual in cohort
    )

    reliable = samples >= min_evidence_samples
    calibration_state = "calibrated" if reliable else "sparse_fallback"

    loo = _leave_one_out_up(cohort)
    brier = _brier_score(loo)
    reliability = _reliability_table(loo)
    reasons = []
    if samples == 0:
        reasons.append(f"no matured samples for horizon {hour}h")
    elif samples < min_evidence_samples:
        reasons.append(
            f"calibration cohort has {samples} samples, below the {min_evidence_samples} evidence threshold"
        )
    return {
        "horizon": f"{hour}h",
        "model_name": model_name,
        "predicted_direction": current_direction,
        "all_samples": all_samples,
        "samples": samples,
        "reliable": reliable,
        "calibration_state": calibration_state,
        "p_up": _round(p_up),
        "p_down": _round(p_down),
        "p_move": _round(p_move),
        "direction_accuracy": _round(direction_accuracy),
        "brier_score": _round(brier),
        "reliability": reliability,
        "calibration_error": reliability["calibration_error"],
        "max_bin_deviation": reliability["max_bin_deviation"],
        "occupied_bins": reliability["occupied_bins"],
        "reasons": reasons,
    }


def build_forecast_probabilities(
    rows: list[dict[str, Any]],
    now: datetime | None = None,
    model_name: str = ENSEMBLE_MODEL,
    horizons: Iterable[int | str] = DEFAULT_HORIZON_HOURS,
    predictions: dict[str, Any] | None = None,
    move_threshold_pct: float = DEFAULT_MOVE_THRESHOLD_PCT,
    neutral_threshold_pct: float = NEUTRAL_THRESHOLD_PCT,
    min_evidence_samples: int = MIN_EVIDENCE_SAMPLES,
    shrinkage_strength: float = SHRINKAGE_STRENGTH,
) -> dict[str, Any]:
    """Build the JSON-serializable per-horizon direction-probability section."""
    if move_threshold_pct < 0:
        raise ValueError("move_threshold_pct must be >= 0")
    if neutral_threshold_pct < 0:
        raise ValueError("neutral_threshold_pct must be >= 0")
    if min_evidence_samples < 1:
        raise ValueError("min_evidence_samples must be >= 1")
    if shrinkage_strength <= 0:
        raise ValueError("shrinkage_strength must be positive")

    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    predictions = predictions if isinstance(predictions, dict) else {}

    horizon_results: dict[str, Any] = {}
    for horizon in horizons:
        hour = _horizon_hours(horizon)
        key = f"{hour}h"
        prediction = predictions.get(key)
        predicted_change_pct = (
            _finite_float(prediction.get("change_pct")) if isinstance(prediction, dict) else None
        )
        horizon_results[key] = horizon_probabilities(
            rows,
            now=current_time,
            model_name=model_name,
            horizon=hour,
            predicted_change_pct=predicted_change_pct,
            move_threshold_pct=move_threshold_pct,
            neutral_threshold_pct=neutral_threshold_pct,
            min_evidence_samples=min_evidence_samples,
            shrinkage_strength=shrinkage_strength,
        )

    reliable = sum(1 for item in horizon_results.values() if item["reliable"])
    scored = [
        float(item["brier_score"])
        for item in horizon_results.values()
        if isinstance(item["brier_score"], (int, float))
    ]
    sample_counts = [int(item["samples"]) for item in horizon_results.values()]
    return {
        "version": DIRECTION_PROBABILITY_VERSION,
        "meaning": (
            "calibrated probability of an up, down, or meaningful move per horizon, "
            "estimated only from matured outcomes available at forecast time"
        ),
        "generated_at": current_time.isoformat(),
        "model_name": model_name,
        "public_claim_allowed": reliable == len(horizon_results),
        "min_evidence_samples": min_evidence_samples,
        "move_threshold_pct": move_threshold_pct,
        "neutral_threshold_pct": neutral_threshold_pct,
        "horizons": horizon_results,
        "overall": {
            "reliable_horizons": reliable,
            "horizon_count": len(horizon_results),
            "min_samples": min(sample_counts) if sample_counts else 0,
            "mean_brier_score": _round(_mean(scored)),
        },
        "leakage_guard": {
            "source": "durable_forecast_history",
            "matured_outcomes_only": True,
            "target_at_cutoff": current_time.isoformat(),
            "forecast_time_inputs": [
                "predicted_change_pct",
                "origin_at",
                "horizon_hours",
                "regime",
            ],
            "outcome_inputs": ["actual_change_pct", "actual_target_price_usd", "target_at"],
        },
    }
