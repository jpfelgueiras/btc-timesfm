"""Cross-horizon forecast coherence for the multi-horizon BTC ensemble.

The production ensemble publishes one quantile forecast (q10/q50/q90) per
2h/4h/8h/16h horizon. Each horizon is produced independently, so taken together
they must still read as a single consistent view of the same underlying move.
This module detects two ways that view breaks:

1. **Quantile-crossing violations** — a torn per-horizon envelope (``q50`` below
   ``q10`` or above ``q90``) or a cross-horizon envelope reversal (the ``q10``
   bound rising, or the ``q90`` bound falling, as the horizon lengthens). These
   can never be published, so a leakage-safe reconciliation step perturbs only
   the offending quantiles by the smallest amount that restores order, bounded
   by guardrails that protect marginal calibration. A crossing that cannot be
   repaired inside those guardrails makes the forecast run fail loudly.
2. **Indefensible direction flips** — an adjacent horizon pair whose medians
   point in opposite directions with both magnitude and matured sample evidence
   to take the flip at face value. Flips without evidence or below the neutral
   noise band are suppressed and logged instead of being published as a signal.

Reconciliation only ever widens a band or re-orders it around the unchanged
median; it never narrows an interval, so per-horizon interval coverage is not
made worse, and both the per-quantile and the interval-width movement are capped
so the conformal/empirical calibration done before this step stays intact.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from btc_timesfm.forecasting.direction_probability import (
    MIN_EVIDENCE_SAMPLES,
    NEUTRAL_THRESHOLD_PCT,
)

MULTI_HORIZON_COHERENCE_VERSION = 1
MIN_QUANTILE_GAP_PCT = 0.05
ENVELOPE_GAP_PCT = 0.05
MAX_QUANTILE_ADJUSTMENT_PCT = 2.0
MAX_WIDTH_GROWTH_REL = 0.20
CROSSING_PENALTY = 0.50
FLIP_PENALTY = 0.15
EPSILON = 1e-9
_QUANTILE_KEYS = ("q10_usd", "q50_usd", "q90_usd")


class CoherenceViolationError(RuntimeError):
    """Raised when forecast quantiles cannot be made coherent inside guardrails."""


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


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


def _sign(value: float) -> int:
    if value > EPSILON:
        return 1
    if value < -EPSILON:
        return -1
    return 0


def _pct_offset(value: float | None, reference: float | None) -> float | None:
    if value is None or reference is None or abs(reference) < EPSILON:
        return None
    return (value / reference - 1.0) * 100.0


def _resolve_base_price(
    predictions: dict[str, Any],
    base_price_usd: float | None,
) -> float | None:
    base = _finite_float(base_price_usd)
    if base is not None:
        return base
    for item in predictions.values():
        if not isinstance(item, dict):
            continue
        price = _finite_float(item.get("price_usd"))
        change_pct = _finite_float(item.get("change_pct"))
        if price is None or change_pct is None or abs(1.0 + change_pct / 100.0) < EPSILON:
            continue
        return price / (1.0 + change_pct / 100.0)
    return None


class _Horizon:
    """Parsed view of one horizon with a small mutable reconciliation state."""

    __slots__ = (
        "key",
        "hour",
        "item",
        "q10",
        "q50",
        "q90",
        "price",
        "samples",
        "final_q10_usd",
        "final_q90_usd",
    )

    def __init__(
        self,
        key: str,
        hour: int,
        item: dict[str, Any],
        q10: float | None,
        q50: float | None,
        q90: float | None,
        price: float | None,
        samples: int,
    ) -> None:
        self.key = key
        self.hour = hour
        self.item = item
        self.q10 = q10
        self.q50 = q50
        self.q90 = q90
        self.price = price
        self.samples = samples
        self.final_q10_usd = q10
        self.final_q90_usd = q90


def _collect_horizons(
    predictions: dict[str, Any],
    samples: dict[str, int] | None,
) -> list[_Horizon]:
    """Parse predictions into horizon-hours-sorted horizons with evidence counts."""
    parsed: list[_Horizon] = []
    samples = samples if isinstance(samples, dict) else {}
    for key, item in predictions.items():
        if not isinstance(item, dict):
            continue
        try:
            hour = _horizon_hours(key)
        except ValueError:
            continue
        q10 = _finite_float(item.get("q10_usd"))
        q50 = _finite_float(item.get("q50_usd"))
        q90 = _finite_float(item.get("q90_usd"))
        price = _finite_float(item.get("price_usd"))
        if q50 is None:
            q50 = price
        evidence = samples.get(key)
        if evidence is None:
            for source in ("calibration_samples", "weighting_samples"):
                value = _finite_float(item.get(source))
                if value is not None:
                    evidence = int(value)
                    break
        if evidence is None:
            evidence = 0
        parsed.append(_Horizon(key, hour, item, q10, q50, q90, price, int(evidence)))
    parsed.sort(key=lambda horizon: horizon.hour)
    return parsed


def _median_move(horizon: _Horizon, base: float | None) -> float | None:
    if horizon.q50 is None:
        return None
    if base is not None:
        return _pct_offset(horizon.q50, base)
    return _finite_float(horizon.item.get("change_pct"))


def _envelope_detail(
    level: str,
    shorter: _Horizon,
    longer: _Horizon,
    base: float | None,
    *,
    rising: bool,
) -> str:
    short_usd = shorter.final_q10_usd if level == "q10_usd" else shorter.final_q90_usd
    long_usd = longer.final_q10_usd if level == "q10_usd" else longer.final_q90_usd
    short_chg = _pct_offset(short_usd, base) if base is not None else None
    long_chg = _pct_offset(long_usd, base) if base is not None else None
    direction = "rises" if rising else "falls"
    return (
        f"{level} {direction} from {shorter.key} "
        f"({_round(short_chg, 3) if short_chg is not None else 'n/a'}%) to "
        f"{longer.key} ({_round(long_chg, 3) if long_chg is not None else 'n/a'}%)"
    )


def _crossing_records(
    horizons: list[_Horizon],
    *,
    base: float | None,
) -> list[dict[str, Any]]:
    """Detect intra-horizon order and cross-horizon envelope violations.

    Reads each horizon's active quantiles (``final_q10_usd``/``final_q90_usd``),
    which equal the original values until reconciliation moves them, so the same
    routine reports both the pre- and post-reconciliation state.
    """
    records: list[dict[str, Any]] = []

    for horizon in horizons:
        q10 = horizon.final_q10_usd
        q90 = horizon.final_q90_usd
        if q10 is None or horizon.q50 is None or q90 is None:
            continue
        lower_gap = _pct_offset(horizon.q50, q10)
        if lower_gap is not None and lower_gap < MIN_QUANTILE_GAP_PCT:
            records.append(
                {
                    "type": "quantile_crossing",
                    "severity": "crossing",
                    "horizon": horizon.key,
                    "level": "q10_usd",
                    "detail": (
                        f"q50_usd - q10_usd gap {_round(lower_gap, 3)}% is below the "
                        f"{MIN_QUANTILE_GAP_PCT}% minimum"
                    ),
                }
            )
        upper_gap = _pct_offset(q90, horizon.q50)
        if upper_gap is not None and upper_gap < MIN_QUANTILE_GAP_PCT:
            records.append(
                {
                    "type": "quantile_crossing",
                    "severity": "crossing",
                    "horizon": horizon.key,
                    "level": "q90_usd",
                    "detail": (
                        f"q90_usd - q50_usd gap {_round(upper_gap, 3)}% is below the "
                        f"{MIN_QUANTILE_GAP_PCT}% minimum"
                    ),
                }
            )

    for shorter, longer in zip(horizons, horizons[1:], strict=False):
        shorter_q10, longer_q10 = shorter.final_q10_usd, longer.final_q10_usd
        if shorter_q10 is not None and longer_q10 is not None and longer_q10 > shorter_q10:
            records.append(
                {
                    "type": "envelope_crossing",
                    "severity": "crossing",
                    "level": "q10_usd",
                    "shorter_horizon": shorter.key,
                    "longer_horizon": longer.key,
                    "detail": _envelope_detail("q10_usd", shorter, longer, base, rising=True),
                }
            )
        shorter_q90, longer_q90 = shorter.final_q90_usd, longer.final_q90_usd
        if shorter_q90 is not None and longer_q90 is not None and longer_q90 < shorter_q90:
            records.append(
                {
                    "type": "envelope_crossing",
                    "severity": "crossing",
                    "level": "q90_usd",
                    "shorter_horizon": shorter.key,
                    "longer_horizon": longer.key,
                    "detail": _envelope_detail("q90_usd", shorter, longer, base, rising=False),
                }
            )
    return records


def _flip_records(
    horizons: list[_Horizon],
    *,
    base: float | None,
    flip_threshold_pct: float,
    min_evidence_samples: int,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for shorter, longer in zip(horizons, horizons[1:], strict=False):
        short_move = _median_move(shorter, base)
        long_move = _median_move(longer, base)
        if short_move is None or long_move is None or _sign(short_move) == 0:
            continue
        if _sign(short_move) == _sign(long_move):
            continue
        magnitude_ok = (
            abs(short_move) >= flip_threshold_pct and abs(long_move) >= flip_threshold_pct
        )
        evidence_ok = (
            shorter.samples >= min_evidence_samples and longer.samples >= min_evidence_samples
        )
        reason = None
        if not magnitude_ok:
            reason = "below_noise_band"
        elif not evidence_ok:
            reason = "sample_poor"
        records.append(
            {
                "type": "direction_flip",
                "severity": "flip",
                "shorter_horizon": shorter.key,
                "longer_horizon": longer.key,
                "shorter_change_pct": _round(short_move, 3),
                "longer_change_pct": _round(long_move, 3),
                "flipped": bool(magnitude_ok and evidence_ok),
                "suppressed": not (magnitude_ok and evidence_ok),
                "suppression_reason": reason,
                "evidence": {
                    "shorter_samples": shorter.samples,
                    "longer_samples": longer.samples,
                    "min_evidence_samples": min_evidence_samples,
                    "flip_threshold_pct": flip_threshold_pct,
                },
            }
        )
    return records


def detect_cross_horizon_violations(
    predictions: dict[str, Any],
    *,
    base_price_usd: float | None = None,
    samples: dict[str, int] | None = None,
    flip_threshold_pct: float = NEUTRAL_THRESHOLD_PCT,
    min_evidence_samples: int = MIN_EVIDENCE_SAMPLES,
) -> dict[str, Any]:
    """Detect crossing and direction-flip violations without reconciling.

    ``base_price_usd`` is only used to express diagnostics as percentage moves;
    detection itself works in price space and never consumes outcome history.
    """
    if flip_threshold_pct < 0:
        raise ValueError("flip_threshold_pct must be >= 0")
    if min_evidence_samples < 1:
        raise ValueError("min_evidence_samples must be >= 1")

    base = _resolve_base_price(predictions, base_price_usd)
    horizons = _collect_horizons(predictions, samples)
    return {
        "crossing_violations": _crossing_records(horizons, base=base),
        "direction_flips": _flip_records(
            horizons,
            base=base,
            flip_threshold_pct=flip_threshold_pct,
            min_evidence_samples=min_evidence_samples,
        ),
        "horizons": {
            horizon.key: {
                "horizon": horizon.key,
                "q10_usd": _round(horizon.q10, 4),
                "q50_usd": _round(horizon.q50, 4),
                "q90_usd": _round(horizon.q90, 4),
                "samples": horizon.samples,
            }
            for horizon in horizons
        },
    }


def _reconcile_targets(
    horizons: list[_Horizon],
    *,
    max_quantile_adjustment_pct: float,
    max_width_growth_rel: float,
) -> dict[str, dict[str, Any]]:
    """Compute final clamped quantile targets and per-horizon outcome info."""
    min_gap_usd = {
        horizon.key: horizon.q50 * MIN_QUANTILE_GAP_PCT / 100.0 if horizon.q50 else 0.0
        for horizon in horizons
    }
    envelope_gap_usd = {
        horizon.key: horizon.q50 * ENVELOPE_GAP_PCT / 100.0 if horizon.q50 else 0.0
        for horizon in horizons
    }

    # 1. Fix torn intra-horizon order around the unchanged median.
    for horizon in horizons:
        if horizon.final_q90_usd is not None and horizon.q50 is not None:
            if horizon.final_q90_usd - horizon.q50 < min_gap_usd[horizon.key]:
                horizon.final_q90_usd = horizon.q50 + min_gap_usd[horizon.key]
        if horizon.final_q10_usd is not None and horizon.q50 is not None:
            if horizon.q50 - horizon.final_q10_usd < min_gap_usd[horizon.key]:
                horizon.final_q10_usd = horizon.q50 - min_gap_usd[horizon.key]

    # 2. Enforce cross-horizon envelope monotonicity (q10 down, q90 up).
    prev_q10: float | None = None
    for horizon in horizons:
        if horizon.q10 is None:
            continue
        if prev_q10 is not None and horizon.final_q10_usd is not None:
            horizon.final_q10_usd = min(
                horizon.final_q10_usd, prev_q10 - envelope_gap_usd[horizon.key]
            )
        prev_q10 = horizon.final_q10_usd

    prev_q90: float | None = None
    for horizon in horizons:
        if horizon.q90 is None:
            continue
        if prev_q90 is not None and horizon.final_q90_usd is not None:
            horizon.final_q90_usd = max(
                horizon.final_q90_usd, prev_q90 + envelope_gap_usd[horizon.key]
            )
        prev_q90 = horizon.final_q90_usd

    # 3. Clamp per-quantile movement and compute width/coverage outcomes.
    outcomes: dict[str, dict[str, Any]] = {}
    for horizon in horizons:
        record: dict[str, Any] = {
            "reconciled": False,
            "residual": False,
            "guardrail_breach": False,
            "delta_q10_pct": None,
            "delta_q90_pct": None,
            "width_pct_before": None,
            "width_pct_after": None,
        }
        if horizon.q10 is None or horizon.q50 is None or horizon.q90 is None:
            outcomes[horizon.key] = record
            continue

        delta_q10 = _pct_offset(horizon.final_q10_usd, horizon.q10)
        delta_q90 = _pct_offset(horizon.final_q90_usd, horizon.q90)
        if delta_q10 is not None and abs(delta_q10) > max_quantile_adjustment_pct:
            shift = math.copysign(max_quantile_adjustment_pct, delta_q10)
            horizon.final_q10_usd = horizon.q10 * (1.0 + shift / 100.0)
        if delta_q90 is not None and abs(delta_q90) > max_quantile_adjustment_pct:
            shift = math.copysign(max_quantile_adjustment_pct, delta_q90)
            horizon.final_q90_usd = horizon.q90 * (1.0 + shift / 100.0)

        delta_q10 = _pct_offset(horizon.final_q10_usd, horizon.q10)
        delta_q90 = _pct_offset(horizon.final_q90_usd, horizon.q90)

        width_before = _pct_offset(horizon.q90, horizon.q10)
        width_after = _pct_offset(horizon.final_q90_usd, horizon.final_q10_usd)
        guardrail_breach = False
        if width_before is not None and width_after is not None and abs(width_before) >= EPSILON:
            guardrail_breach = width_after > width_before * (1.0 + max_width_growth_rel)

        needs_reconcile = bool(
            (delta_q10 is not None and abs(delta_q10) > EPSILON)
            or (delta_q90 is not None and abs(delta_q90) > EPSILON)
        )
        record.update(
            {
                "reconciled": needs_reconcile,
                "residual": bool(guardrail_breach),
                "guardrail_breach": guardrail_breach,
                "delta_q10_pct": _round(delta_q10, 4),
                "delta_q90_pct": _round(delta_q90, 4),
                "width_pct_before": _round(width_before, 4),
                "width_pct_after": _round(width_after, 4),
            }
        )
        outcomes[horizon.key] = record
    return outcomes


def _signature(record: dict[str, Any]) -> tuple[str, str]:
    if record["type"] == "quantile_crossing":
        return (str(record["horizon"]), str(record["level"]))
    return (
        f"{record.get('shorter_horizon')}|{record.get('longer_horizon')}",
        str(record.get("level", "")),
    )


def reconcile_forecast_coherence(
    predictions: dict[str, Any],
    *,
    base_price_usd: float | None = None,
    samples: dict[str, int] | None = None,
    flip_threshold_pct: float = NEUTRAL_THRESHOLD_PCT,
    min_evidence_samples: int = MIN_EVIDENCE_SAMPLES,
    max_quantile_adjustment_pct: float = MAX_QUANTILE_ADJUSTMENT_PCT,
    max_width_growth_rel: float = MAX_WIDTH_GROWTH_REL,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reconcile quantiles toward coherence and emit the coherence section.

    Only current-forecast quantiles are ever moved (leakage-safe); the median and
    the ``price_usd`` point forecast are never changed. Quantiles are perturbed by
    the smallest amount that removes a crossing and every movement is capped by
    ``max_quantile_adjustment_pct`` unless the interval would grow by more than
    ``max_width_growth_rel``, which would degrade marginal calibration.

    Returns ``{"reconciled_predictions": ..., "section": ...}``.
    """
    if flip_threshold_pct < 0:
        raise ValueError("flip_threshold_pct must be >= 0")
    if min_evidence_samples < 1:
        raise ValueError("min_evidence_samples must be >= 1")
    if max_quantile_adjustment_pct < 0:
        raise ValueError("max_quantile_adjustment_pct must be >= 0")
    if max_width_growth_rel < 0:
        raise ValueError("max_width_growth_rel must be >= 0")

    base = _resolve_base_price(predictions, base_price_usd)
    horizons = _collect_horizons(predictions, samples)
    crossing_original = _crossing_records(horizons, base=base)
    flips = _flip_records(
        horizons,
        base=base,
        flip_threshold_pct=flip_threshold_pct,
        min_evidence_samples=min_evidence_samples,
    )
    outcomes = _reconcile_targets(
        horizons,
        max_quantile_adjustment_pct=max_quantile_adjustment_pct,
        max_width_growth_rel=max_width_growth_rel,
    )
    crossing_final = _crossing_records(horizons, base=base)
    residual_signatures = {_signature(record) for record in crossing_final}

    violations: list[dict[str, Any]] = []
    for record in crossing_original:
        residual = _signature(record) in residual_signatures
        violations.append(
            {
                **record,
                "reconciled": bool(not residual),
                "residual": bool(residual),
                "unrecoverable": bool(residual),
            }
        )

    guardrail_entries: list[dict[str, Any]] = []
    for key, outcome in outcomes.items():
        if not outcome.get("guardrail_breach"):
            continue
        guardrail_entries.append(
            {
                "type": "coverage_guardrail",
                "severity": "crossing",
                "horizon": key,
                "detail": (
                    f"reconciliation grows the q10-q90 interval beyond "
                    f"{_round(max_width_growth_rel * 100, 3)}% of its pre-"
                    "reconciliation width, which would degrade calibration"
                ),
                "reconciled": False,
                "residual": True,
                "unrecoverable": True,
            }
        )

    froze_widths_for_section = {key: outcome for key, outcome in outcomes.items()}
    reconciled_predictions = _build_reconciled_predictions(predictions, horizons)
    section = _build_section(
        horizons=horizons,
        violations=violations + guardrail_entries,
        flips=flips,
        outcomes=froze_widths_for_section,
        flip_threshold_pct=flip_threshold_pct,
        min_evidence_samples=min_evidence_samples,
        max_quantile_adjustment_pct=max_quantile_adjustment_pct,
        max_width_growth_rel=max_width_growth_rel,
        now=now,
    )
    return {"reconciled_predictions": reconciled_predictions, "section": section}


def _build_reconciled_predictions(
    predictions: dict[str, Any],
    horizons: list[_Horizon],
) -> dict[str, Any]:
    by_key = {horizon.key: horizon for horizon in horizons}
    reconciled: dict[str, Any] = {}
    for key, item in predictions.items():
        if not isinstance(item, dict):
            reconciled[key] = item
            continue
        updated = dict(item)
        horizon = by_key.get(key)
        if horizon is not None:
            updated["q10_usd"] = _round(horizon.final_q10_usd, 4)
            updated["q90_usd"] = _round(horizon.final_q90_usd, 4)
            if horizon.q50 is not None:
                updated["q50_usd"] = _round(horizon.q50, 4)
        reconciled[key] = updated
    return reconciled


def _build_section(
    *,
    horizons: list[_Horizon],
    violations: list[dict[str, Any]],
    flips: list[dict[str, Any]],
    outcomes: dict[str, dict[str, Any]],
    flip_threshold_pct: float,
    min_evidence_samples: int,
    max_quantile_adjustment_pct: float,
    max_width_growth_rel: float,
    now: datetime | None,
) -> dict[str, Any]:
    crossing_records = [record for record in violations if record["severity"] == "crossing"]
    unrecoverable = sum(
        1 for record in crossing_records if record.get("unrecoverable") or record.get("residual")
    )
    reconciled_entries = sum(
        1 for record in crossing_records if record.get("reconciled") and not record.get("residual")
    )
    evidenced_flips = sum(1 for record in flips if record.get("flipped"))
    suppressed_flips = sum(1 for record in flips if record.get("suppressed"))

    score = 1.0 - CROSSING_PENALTY * unrecoverable - FLIP_PENALTY * evidenced_flips
    score = max(0.0, min(1.0, score))

    horizon_rows: dict[str, Any] = {}
    for horizon in horizons:
        outcome = outcomes.get(horizon.key, {})
        horizon_rows[horizon.key] = {
            "horizon": horizon.key,
            "q10_usd": _round(horizon.q10, 4),
            "q50_usd": _round(horizon.q50, 4),
            "q90_usd": _round(horizon.q90, 4),
            "reconciled_q10_usd": _round(horizon.final_q10_usd, 4),
            "reconciled_q90_usd": _round(horizon.final_q90_usd, 4),
            "reconciliation_delta_q10_pct": outcome.get("delta_q10_pct"),
            "reconciliation_delta_q90_pct": outcome.get("delta_q90_pct"),
            "width_pct_before": outcome.get("width_pct_before"),
            "width_pct_after": outcome.get("width_pct_after"),
            "samples": horizon.samples,
            "evidence_reliable": horizon.samples >= min_evidence_samples,
        }

    generated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "version": MULTI_HORIZON_COHERENCE_VERSION,
        "meaning": (
            "cross-horizon quantile coherence of the 2h/4h/8h/16h ensemble: "
            "no crossing q10/q50/q90 envelopes, no indefensible direction flips, "
            "and quantile adjustments bounded to preserve marginal calibration"
        ),
        "generated_at": generated_at.isoformat(),
        "coherent": bool(unrecoverable == 0 and evidenced_flips == 0),
        "coherence_score": _round(score, 3),
        "public_claim_allowed": bool(unrecoverable == 0 and evidenced_flips == 0),
        "horizons": horizon_rows,
        "guardrails": {
            "min_quantile_gap_pct": MIN_QUANTILE_GAP_PCT,
            "envelope_gap_pct": ENVELOPE_GAP_PCT,
            "max_quantile_adjustment_pct": max_quantile_adjustment_pct,
            "max_width_growth_rel": max_width_growth_rel,
            "flip_threshold_pct": flip_threshold_pct,
            "min_evidence_samples": min_evidence_samples,
            "coverage_preservation": (
                "reconciliation only widens or re-orders quantiles around the "
                "unchanged median; interval width may grow by at most "
                f"{_round(max_width_growth_rel * 100, 3)}% relative to its "
                "pre-reconciliation value and no single quantile may move more "
                f"than {_round(max_quantile_adjustment_pct, 3)}% of its value"
            ),
        },
        "violations": violations + flips,
        "violation_log": {
            "crossing_entries": len(crossing_records),
            "reconciled_crossings": reconciled_entries,
            "unrecoverable_crossings": unrecoverable,
            "evidenced_direction_flips": evidenced_flips,
            "suppressed_direction_flips": suppressed_flips,
            "coherence_score": _round(score, 3),
        },
        "leakage_guard": {
            "source": "current_ensemble_forecast_quantiles_only",
            "matured_outcomes_only": True,
            "reconciliation_inputs": list(_QUANTILE_KEYS),
            "evidence_inputs": ["matured_sample_counts_available_at_forecast_time"],
            "outcome_inputs": [],
            "forecast_time_cutoff": generated_at.isoformat(),
            "note": (
                "reconciliation perturbs only current forecast quantiles; no "
                "outcome realized after the forecast-time cutoff participates"
            ),
        },
    }


def assert_forecast_coherent(
    predictions: dict[str, Any],
    *,
    base_price_usd: float | None = None,
    samples: dict[str, int] | None = None,
    flip_threshold_pct: float = NEUTRAL_THRESHOLD_PCT,
    min_evidence_samples: int = MIN_EVIDENCE_SAMPLES,
    max_quantile_adjustment_pct: float = MAX_QUANTILE_ADJUSTMENT_PCT,
    max_width_growth_rel: float = MAX_WIDTH_GROWTH_REL,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reconcile and fail loudly if any quantile crossing remains unrecoverable.

    Returns the same ``{"reconciled_predictions", "section"}`` as
    :func:`reconcile_forecast_coherence` when the forecast is coherent inside the
    guardrails, and raises :class:`CoherenceViolationError` otherwise.
    """
    result = reconcile_forecast_coherence(
        predictions,
        base_price_usd=base_price_usd,
        samples=samples,
        flip_threshold_pct=flip_threshold_pct,
        min_evidence_samples=min_evidence_samples,
        max_quantile_adjustment_pct=max_quantile_adjustment_pct,
        max_width_growth_rel=max_width_growth_rel,
        now=now,
    )
    log = result["section"]["violation_log"]
    if int(log["unrecoverable_crossings"]) > 0:
        details = [
            record
            for record in result["section"]["violations"]
            if record["severity"] == "crossing"
            and (record.get("unrecoverable") or record.get("residual"))
        ]
        raise CoherenceViolationError(
            "forecast quantiles are not coherent within guardrails: "
            f"{int(log['unrecoverable_crossings'])} unrecoverable crossing(s); "
            f"details={details}"
        )
    return result
