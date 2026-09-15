"""Horizon-specific dynamic neutral and no-edge thresholds.

The ensemble uses a symmetric percentage band to decide whether a predicted BTC
move is up, down, or neutral. Issue #113 ships a calibrated probability estimate
for each horizon, and the edge-attribution work (#110/#111) measures whether the
ensemble actually beats persistence. This module learns the *size* of that
neutral/no-edge band per horizon instead of reusing one fixed threshold.

For every 2h/4h/8h/16h horizon the learned band is derived only from matured
durable-history rows whose ``target_at`` already passed at the forecast origin:

- realized volatility (sample standard deviation of realized percentage moves),
- the standard error of the mean realized move (statistical evidence via a
  one-sample mean test against zero),
- the calibrated directional probabilities ``P(up)``/``P(down)`` and a
  binomial-style difference test against chance (0.5/0.5),
- minimum sample/evidence safeguards.

The neutral threshold scales with realized volatility and shrinks as matured
evidence accumulates: ``neutral = z_95 * sigma / sqrt(n)`` and the stricter
no-edge threshold ``no_edge = z_99 * sigma / sqrt(n)``. When evidence is
insufficient the fixed 0.25% threshold is used as a fallback so behavior
degrades safely instead of emitting an overconfident narrow band. Each
per-horizon block also classifies against the fixed-threshold baseline so the
learned neutral zone can be compared with the old static rule.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Iterable

from btc_timesfm.forecasting.direction_probability import (
    classify_direction,
    horizon_probabilities,
)
from btc_timesfm.history.history_store import ENSEMBLE_MODEL

DYNAMIC_THRESHOLD_VERSION = 1
HORIZONS = ("2h", "4h", "8h", "16h")
DEFAULT_HORIZON_HOURS = (2, 4, 8, 16)
MIN_EVIDENCE_SAMPLES = 20
FIXED_NEUTRAL_THRESHOLD_PCT = 0.25
MIN_NEUTRAL_THRESHOLD_PCT = 0.05
MAX_NEUTRAL_THRESHOLD_PCT = 2.0
MAX_NO_EDGE_THRESHOLD_PCT = 4.0
SIGNIFICANCE_ALPHA = 0.05
NO_EDGE_MULTIPLIER = 2.0
Z_95 = 1.959963984540054
Z_99 = 2.5758293035489004
EPSILON = 1e-9


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None else None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return sum(items) / len(items) if items else None


def _std(values: Iterable[float]) -> float | None:
    items = list(values)
    if len(items) < 2:
        return None
    mean = sum(items) / len(items)
    variance = sum((value - mean) ** 2 for value in items) / (len(items) - 1)
    if variance <= 0.0:
        return 0.0
    return math.sqrt(variance)


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


def _two_sided_pvalue(z: float) -> float:
    return math.erfc(abs(z) / math.sqrt(2.0))


def _select_rows(
    rows: list[dict[str, Any]],
    *,
    now: datetime,
    model_name: str,
    horizon_hours: int,
) -> list[tuple[float, float]]:
    """Return (predicted_change_pct, actual_change_pct) pairs available at ``now``.

    A row only counts when it belongs to the model/horizon and its outcome was
    already realized at the forecast-time cutoff (``target_at <= now``), so no
    information observed after the forecast can leak into the thresholds.
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


def _drift_statistics(
    actuals: Iterable[float],
    alpha: float,
) -> dict[str, Any]:
    """One-sample mean test of realized moves against zero (normal approximation)."""
    values = list(actuals)
    if not values:
        return {
            "samples": 0,
            "mean_change_pct": None,
            "realized_volatility_pct": None,
            "standard_error_pct": None,
            "t_statistic": None,
            "p_value": None,
            "conclusion": "no_samples",
        }
    mean = sum(values) / len(values)
    volatility = _std(values)
    if volatility is None or volatility <= EPSILON:
        return {
            "samples": len(values),
            "mean_change_pct": _round(mean),
            "realized_volatility_pct": _round(volatility),
            "standard_error_pct": None,
            "t_statistic": None,
            "p_value": None,
            "conclusion": "insufficient_variance",
        }
    standard_error = volatility / math.sqrt(len(values))
    t_statistic = mean / standard_error
    p_value = _two_sided_pvalue(t_statistic)
    conclusion = "drift_detected" if p_value < alpha else "no_detectable_drift"
    return {
        "samples": len(values),
        "mean_change_pct": _round(mean),
        "realized_volatility_pct": _round(volatility),
        "standard_error_pct": _round(standard_error),
        "t_statistic": _round(t_statistic),
        "p_value": _round(p_value),
        "conclusion": conclusion,
    }


def _proportion_edge_test(
    n_up: int,
    n_down: int,
    *,
    alpha: float,
    min_evidence_samples: int,
) -> dict[str, Any]:
    """Difference-of-counts test of P(up) against P(down) led to chance."""
    n_sign = n_up + n_down
    if n_sign == 0:
        return {
            "n_sign": 0,
            "probability_edge_z": None,
            "probability_edge_p_value": None,
            "conclusion": "no_signed_outcomes",
        }
    if n_sign < min_evidence_samples:
        return {
            "n_sign": n_sign,
            "probability_edge_z": None,
            "probability_edge_p_value": None,
            "conclusion": "insufficient_samples",
        }
    z = abs(n_up - n_down) / math.sqrt(n_sign)
    p_value = _two_sided_pvalue(z)
    conclusion = (
        "directional_probability_edge" if p_value < alpha else "no_directional_probability_edge"
    )
    return {
        "n_sign": n_sign,
        "probability_edge_z": _round(z),
        "probability_edge_p_value": _round(p_value),
        "conclusion": conclusion,
    }


def evaluate_against_fixed(
    rows: list[tuple[float, float]],
    *,
    neutral_threshold_pct: float,
    fixed_threshold_pct: float = FIXED_NEUTRAL_THRESHOLD_PCT,
) -> dict[str, Any]:
    """Compare learned and fixed neutral classification on the same matured rows."""
    if neutral_threshold_pct < 0:
        raise ValueError("neutral_threshold_pct must be >= 0")
    if fixed_threshold_pct < 0:
        raise ValueError("fixed_threshold_pct must be >= 0")

    baseline: dict[str, Any] = {
        "samples": 0,
        "fixed_threshold_pct": fixed_threshold_pct,
        "dynamic_threshold_pct": neutral_threshold_pct,
        "fixed_neutral_rate": None,
        "dynamic_neutral_rate": None,
        "agreement_rate": None,
        "reclassified_directional_to_neutral": 0,
        "reclassified_neutral_to_directional": 0,
        "fixed_direction_accuracy": None,
        "dynamic_direction_accuracy": None,
    }
    if not rows:
        return baseline

    fixed_labels = [classify_direction(actual, fixed_threshold_pct) for _, actual in rows]
    dynamic_labels = [classify_direction(actual, neutral_threshold_pct) for _, actual in rows]
    agreement = sum(f == d for f, d in zip(fixed_labels, dynamic_labels, strict=True))
    directional_active = {"up", "down"}

    def direction_accuracy(labels: list[str]) -> float | None:
        correct = 0
        for (predicted, _), label in zip(rows, labels, strict=True):
            if classify_direction(predicted, neutral_threshold_pct) == label:
                correct += 1
        return correct / len(rows)

    fixed_neutral_rate = (
        sum(1 for label in fixed_labels if label == "neutral") / len(rows) if rows else None
    )
    dynamic_neutral_rate = (
        sum(1 for label in dynamic_labels if label == "neutral") / len(rows) if rows else None
    )
    reclassified_directional_to_neutral = sum(
        f in directional_active and d == "neutral"
        for f, d in zip(fixed_labels, dynamic_labels, strict=True)
    )
    reclassified_neutral_to_directional = sum(
        f == "neutral" and d in directional_active
        for f, d in zip(fixed_labels, dynamic_labels, strict=True)
    )

    baseline.update(
        {
            "samples": len(rows),
            "fixed_neutral_rate": _round(fixed_neutral_rate),
            "dynamic_neutral_rate": _round(dynamic_neutral_rate),
            "agreement_rate": _round(agreement / len(rows)),
            "reclassified_directional_to_neutral": reclassified_directional_to_neutral,
            "reclassified_neutral_to_directional": reclassified_neutral_to_directional,
            "fixed_direction_accuracy": _round(direction_accuracy(fixed_labels)),
            "dynamic_direction_accuracy": _round(direction_accuracy(dynamic_labels)),
        }
    )
    return baseline


def horizon_threshold_state(
    rows: list[dict[str, Any]],
    *,
    now: datetime,
    model_name: str = ENSEMBLE_MODEL,
    horizon: int | str,
    predicted_change_pct: float | None = None,
    min_evidence_samples: int = MIN_EVIDENCE_SAMPLES,
    fixed_neutral_threshold_pct: float = FIXED_NEUTRAL_THRESHOLD_PCT,
    significance_alpha: float = SIGNIFICANCE_ALPHA,
    min_neutral_threshold_pct: float = MIN_NEUTRAL_THRESHOLD_PCT,
    max_neutral_threshold_pct: float = MAX_NEUTRAL_THRESHOLD_PCT,
    max_no_edge_threshold_pct: float = MAX_NO_EDGE_THRESHOLD_PCT,
) -> dict[str, Any]:
    """Learn the neutral/no-edge thresholds for one model+horizon at ``now``."""
    if min_evidence_samples < 1:
        raise ValueError("min_evidence_samples must be >= 1")
    if fixed_neutral_threshold_pct < 0:
        raise ValueError("fixed_neutral_threshold_pct must be >= 0")
    if not 0.0 < significance_alpha < 1.0:
        raise ValueError("significance_alpha must be between 0 and 1")
    if min_neutral_threshold_pct < 0 or max_neutral_threshold_pct <= min_neutral_threshold_pct:
        raise ValueError("neutral threshold bounds must be positive with min < max")

    hour = _horizon_hours(horizon)
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    selected = _select_rows(
        rows,
        now=current_time,
        model_name=model_name,
        horizon_hours=hour,
    )
    samples = len(selected)
    actuals = [actual for _, actual in selected]
    predicted = _finite_float(predicted_change_pct)

    n_up = sum(1 for actual in actuals if actual > EPSILON)
    n_down = sum(1 for actual in actuals if actual < -EPSILON)
    drift = _drift_statistics(actuals, significance_alpha)
    proportion = _proportion_edge_test(
        n_up,
        n_down,
        alpha=significance_alpha,
        min_evidence_samples=min_evidence_samples,
    )

    volatility = drift.get("realized_volatility_pct")
    reliable = samples >= min_evidence_samples
    threshold_reasons: list[str] = []
    if reliable and isinstance(volatility, (int, float)) and volatility > EPSILON:
        neutral = Z_95 * float(volatility) / math.sqrt(samples)
        no_edge = Z_99 * float(volatility) / math.sqrt(samples)
        source = "learned"
    else:
        neutral = float(fixed_neutral_threshold_pct)
        no_edge = neutral * NO_EDGE_MULTIPLIER
        source = "fixed_fallback"
        if not reliable:
            threshold_reasons.append(
                f"{samples} matured samples below the {min_evidence_samples} evidence minimum"
            )
        elif volatility is None or float(volatility or 0.0) <= EPSILON:
            threshold_reasons.append("realized volatility is undefined or zero")

    neutral = _clamp(neutral, min_neutral_threshold_pct, max_neutral_threshold_pct)
    no_edge = _clamp(no_edge, neutral, max_no_edge_threshold_pct)

    predicted_class = (
        classify_direction(predicted, neutral) if predicted is not None else "no_prediction"
    )

    probability_conclusion = str(proportion.get("conclusion") or "insufficient_samples")
    suppress_reasons: list[str] = []
    if not reliable:
        suppress_reasons.append(
            f"only {samples} matured samples, below the {min_evidence_samples} evidence minimum"
        )
    if predicted is not None and abs(predicted) <= no_edge + EPSILON:
        suppress_reasons.append("predicted move is inside the no-edge noise band")
    if probability_conclusion == "no_directional_probability_edge":
        suppress_reasons.append(
            "calibrated directional probabilities are indistinguishable from chance"
        )
    elif probability_conclusion == "no_signed_outcomes":
        suppress_reasons.append("no matured directional outcomes are available")

    if not reliable:
        edge_status = "insufficient_evidence"
    elif suppress_reasons:
        edge_status = "no_edge"
    else:
        edge_status = "edge"

    suppress = edge_status != "edge"

    calibrated = horizon_probabilities(
        rows,
        now=current_time,
        model_name=model_name,
        horizon=hour,
        predicted_change_pct=None,
        min_evidence_samples=min_evidence_samples,
    )
    baseline = evaluate_against_fixed(
        selected,
        neutral_threshold_pct=neutral,
        fixed_threshold_pct=fixed_neutral_threshold_pct,
    )

    return {
        "horizon": f"{hour}h",
        "model_name": model_name,
        "samples": samples,
        "reliable": reliable,
        "realized_volatility_pct": _round(volatility),
        "mean_actual_change_pct": drift.get("mean_change_pct"),
        "neutral_threshold_pct": _round(neutral),
        "no_edge_threshold_pct": _round(no_edge),
        "threshold_source": source,
        "threshold_reasons": threshold_reasons,
        "predicted_change_pct": _round(predicted),
        "predicted_class": predicted_class,
        "edge_status": edge_status,
        "suppress_directional_claim": suppress,
        "suppress_reasons": suppress_reasons,
        "statistical_evidence": {
            "alpha": significance_alpha,
            "samples": drift.get("samples"),
            "mean_change_pct": drift.get("mean_change_pct"),
            "standard_error_pct": drift.get("standard_error_pct"),
            "t_statistic": drift.get("t_statistic"),
            "p_value": drift.get("p_value"),
            "conclusion": drift.get("conclusion"),
        },
        "calibrated_evidence": {
            "p_up": calibrated.get("p_up"),
            "p_down": calibrated.get("p_down"),
            "p_move": calibrated.get("p_move"),
            "calibration_state": calibrated.get("calibration_state"),
            "n_up": n_up,
            "n_down": n_down,
            "n_sign": proportion.get("n_sign"),
            "probability_edge_z": proportion.get("probability_edge_z"),
            "probability_edge_p_value": proportion.get("probability_edge_p_value"),
            "probability_conclusion": probability_conclusion,
        },
        "baseline_evaluation": baseline,
    }


def build_dynamic_threshold_section(
    rows: list[dict[str, Any]],
    now: datetime | None = None,
    model_name: str = ENSEMBLE_MODEL,
    horizons: Iterable[int | str] = DEFAULT_HORIZON_HOURS,
    predictions: dict[str, Any] | None = None,
    min_evidence_samples: int = MIN_EVIDENCE_SAMPLES,
    fixed_neutral_threshold_pct: float = FIXED_NEUTRAL_THRESHOLD_PCT,
    significance_alpha: float = SIGNIFICANCE_ALPHA,
    min_neutral_threshold_pct: float = MIN_NEUTRAL_THRESHOLD_PCT,
    max_neutral_threshold_pct: float = MAX_NEUTRAL_THRESHOLD_PCT,
    max_no_edge_threshold_pct: float = MAX_NO_EDGE_THRESHOLD_PCT,
) -> dict[str, Any]:
    """Build the JSON-serializable per-horizon dynamic-threshold section."""
    if min_evidence_samples < 1:
        raise ValueError("min_evidence_samples must be >= 1")
    if fixed_neutral_threshold_pct < 0:
        raise ValueError("fixed_neutral_threshold_pct must be >= 0")
    if not 0.0 < significance_alpha < 1.0:
        raise ValueError("significance_alpha must be between 0 and 1")

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
        horizon_results[key] = horizon_threshold_state(
            rows,
            now=current_time,
            model_name=model_name,
            horizon=hour,
            predicted_change_pct=predicted_change_pct,
            min_evidence_samples=min_evidence_samples,
            fixed_neutral_threshold_pct=fixed_neutral_threshold_pct,
            significance_alpha=significance_alpha,
            min_neutral_threshold_pct=min_neutral_threshold_pct,
            max_neutral_threshold_pct=max_neutral_threshold_pct,
            max_no_edge_threshold_pct=max_no_edge_threshold_pct,
        )

    suppressed = [
        key for key, item in horizon_results.items() if item["suppress_directional_claim"]
    ]
    reliable = sum(1 for item in horizon_results.values() if item["reliable"])
    volatilities = [
        float(item["realized_volatility_pct"])
        for item in horizon_results.values()
        if item["realized_volatility_pct"] is not None
    ]
    return {
        "version": DYNAMIC_THRESHOLD_VERSION,
        "meaning": (
            "per-horizon thresholds below which expected BTC moves are statistically "
            "indistinguishable from noise, learned from matured outcomes and "
            "realized volatility available at forecast time"
        ),
        "generated_at": current_time.isoformat(),
        "model_name": model_name,
        "min_evidence_samples": min_evidence_samples,
        "significance_alpha": significance_alpha,
        "fixed_neutral_threshold_pct": fixed_neutral_threshold_pct,
        "public_directional_claim_allowed": not suppressed,
        "suppressed_horizons": suppressed,
        "horizons": horizon_results,
        "overall": {
            "reliable_horizons": reliable,
            "suppressed_horizons": len(suppressed),
            "horizon_count": len(horizon_results),
            "mean_realized_volatility_pct": _round(_mean(volatilities)),
        },
        "leakage_guard": {
            "source": "durable_forecast_history",
            "matured_outcomes_only": True,
            "target_at_cutoff": current_time.isoformat(),
            "threshold_inputs": ["actual_change_pct"],
            "forecast_time_inputs": [
                "predicted_change_pct",
                "origin_at",
                "horizon_hours",
            ],
            "outcome_inputs": ["actual_change_pct", "actual_target_price_usd", "target_at"],
        },
    }
