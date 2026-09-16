"""Regime- and volatility-conditional interval calibration (#155).

Production intervals are calibrated marginally today: the conformal multiplier
restores ~80% coverage on average, but a market currently in a regime or
volatility state where intervals under-cover keeps being under-covered. This
module re-calibrates the conformal *interval* adjustment per
``(regime, realized-volatility)`` bucket.

Bucket assignment uses only origin-time information:

- the regime label that was attached to the snapshot at forecast time;
- the short-window realized volatility of the dollar move (``market_features``
  ``volatility_6h_pct`` by default, with fallbacks to other origin-time vol
  features) that was known when the forecast was created.

Per-bucket conformal multipliers reuse the same normalized-residual machinery
as the marginal ``conformal_calibration`` module. A bucket is trusted only once
it has at least ``min_samples`` (default 20) matured samples; sparse buckets are
shrunk back toward the marginal multiplier and always report their sample size,
coverage and shrinkage so they never silently claim precision.

The module is the persistent, drift-resilient recalibration path: it keeps no
mutable state, recomputes only from matured snapshots that predate the current
origin (``now``), and can therefore be re-run on every forecast (and shared with
later rolling-recalibration work, #159). The v2 items #115 (dynamic no-edge) and
#131 (uncertainty-aware weighting) build on the per-bucket diagnostic surface
exposed here.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import numpy as np

from btc_timesfm.forecasting.conformal_calibration import (
    DEFAULT_HISTORY_LIMIT,
    DEFAULT_MIN_SAMPLES,
    DEFAULT_TARGET_COVERAGE,
    _conformal_multiplier,
    _legacy_multiplier,
    _sample,
)

CONDITIONAL_CALIBRATION_VERSION = 2
DEFAULT_HORIZONS = (2, 4, 8, 16)
DEFAULT_SEGMENT_DIMENSIONS = ("regime", "volatility", "liquidity", "data_quality")
DEFAULT_AGGREGATE_COVERAGE_TOLERANCE = 0.05
DEFAULT_VOLATILITY_FEATURE_KEYS = (
    "volatility_6h_pct",
    "realized_vol_6h_pct",
    "rv_6h_pct",
    "volatility_24h_pct",
)
_DEFAULT_VOL_BUCKET_CUTS = (0.75, 1.50)
_DEFAULT_COVERAGE_TOLERANCE = 0.10
_EPSILON = 1e-9


def _parse_vol_cuts(raw: str | None) -> tuple[float, float]:
    if not raw:
        return _DEFAULT_VOL_BUCKET_CUTS
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if len(parts) != 2:
        return _DEFAULT_VOL_BUCKET_CUTS
    try:
        first, second = float(parts[0]), float(parts[1])
    except ValueError:
        return _DEFAULT_VOL_BUCKET_CUTS
    if first <= 0.0 or second <= first:
        return _DEFAULT_VOL_BUCKET_CUTS
    return (first, second)


def _parse_tolerance(raw: str | None) -> float:
    if not raw:
        return _DEFAULT_COVERAGE_TOLERANCE
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_COVERAGE_TOLERANCE
    return value if value > 0.0 else _DEFAULT_COVERAGE_TOLERANCE


DEFAULT_VOL_BUCKET_CUTS = _parse_vol_cuts(os.getenv("BTC_CONDITIONAL_VOL_CUTS"))
DEFAULT_COVERAGE_TOLERANCE = _parse_tolerance(os.getenv("BTC_CONDITIONAL_COVERAGE_TOLERANCE"))


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None else None


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _manifest_id(payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(_canonical_json(payload).encode()).hexdigest()[:16]
    return f"conditional-calibration-v{CONDITIONAL_CALIBRATION_VERSION}-{digest}"


def _validate_config(
    target_coverage: float,
    history_limit: int,
    min_samples: int,
    tolerance: float,
    cuts: tuple[float, float],
) -> None:
    if not 0.5 < target_coverage < 1.0:
        raise ValueError("target_coverage must be between 0.5 and 1.0")
    if history_limit < 1:
        raise ValueError("history_limit must be positive")
    if min_samples < 1:
        raise ValueError("min_samples must be positive")
    if tolerance <= 0.0:
        raise ValueError("tolerance must be positive")
    if len(cuts) != 2 or cuts[0] <= 0.0 or cuts[1] <= cuts[0]:
        raise ValueError("vol_bucket_cuts must be an increasing positive pair")


def _origin_time(snapshot: dict[str, Any]) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(snapshot["latest_close_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def realized_volatility_pct(
    snapshot: dict[str, Any],
    feature_keys: Iterable[str] = DEFAULT_VOLATILITY_FEATURE_KEYS,
) -> float | None:
    """Short-window realized volatility of the dollar move at the origin.

    Only feature fields that were already known at forecast time are read. The
    6h realized-volatility feature is preferred because it is the short window;
    older snapshots fall back to other origin-time vol features so they can
    still be bucketed honestly rather than dropped.
    """
    features = snapshot.get("market_features")
    if not isinstance(features, dict):
        return None
    for key in feature_keys:
        value = _finite_float(features.get(key))
        if value is not None and value > 0.0:
            return value
    return None


def realized_vol_bucket(
    volatility_pct: float | None, cuts: tuple[float, float] = DEFAULT_VOL_BUCKET_CUTS
) -> str:
    """Map a short-window realized-vol percent to a low/medium/high bucket."""
    if volatility_pct is None:
        return "unknown"
    if volatility_pct < cuts[0]:
        return "low"
    if volatility_pct < cuts[1]:
        return "medium"
    return "high"


def bucket_label(regime: str, volatility_bucket: str) -> str:
    """Compose a compact bucket key, e.g. ``range:low``."""
    return f"{regime}:{volatility_bucket}"


def assign_bucket(
    snapshot: dict[str, Any], cuts: tuple[float, float] = DEFAULT_VOL_BUCKET_CUTS
) -> str:
    """Assign a snapshot to a (regime, realized-vol) bucket using origin-time data only.

    The outcome/target fields (``_outcomes``, ``predictions``) are never read, so
    reassignment is provably origin-time-only: two snapshots that share the same
    origin-time state land in the same bucket no matter how their realized
    outcomes differ.
    """
    regime = str(snapshot.get("regime") or "unknown")
    return bucket_label(regime, realized_vol_bucket(realized_volatility_pct(snapshot), cuts))


def _bucket_sample(
    snapshot: dict[str, Any],
    actual_by_timestamp: dict[int, float],
    hour: int,
    cuts: tuple[float, float],
) -> dict[str, Any] | None:
    base = _sample(snapshot, actual_by_timestamp, hour)
    if base is None:
        return None
    return {**base, "bucket": assign_bucket(snapshot, cuts)}


def collect_bucket_samples(
    history: list[dict[str, Any]],
    actual_by_timestamp: dict[int, float],
    hour: int,
    *,
    bucket: str | None = None,
    history_limit: int = DEFAULT_HISTORY_LIMIT,
    cuts: tuple[float, float] = DEFAULT_VOL_BUCKET_CUTS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return newest matured normalized-residual samples, optionally per bucket.

    Only snapshots that are matured at the origin (their actual target candle is
    already known) participate, and only snapshots whose origin is at or before
    ``now`` when a cutoff is supplied. When ``bucket`` is given, the full history
    is scanned so a rare bucket can still accumulate up to ``history_limit`` of
    its own matured samples.
    """
    samples: list[dict[str, Any]] = []
    for snapshot in reversed(history):
        if bucket is not None and assign_bucket(snapshot, cuts) != bucket:
            continue
        if now is not None:
            origin = _origin_time(snapshot)
            if origin is None or origin + timedelta(hours=hour) > now:
                continue
        sample = _bucket_sample(snapshot, actual_by_timestamp, hour, cuts)
        if sample is None:
            continue
        samples.append(sample)
        if len(samples) >= history_limit:
            break
    samples.reverse()
    return samples


def empirical_coverage(samples: list[dict[str, Any]], multiplier: float) -> float | None:
    """Share of samples whose normalized residual fits inside ``multiplier``."""
    if not samples:
        return None
    return float(np.mean([sample["score"] <= multiplier for sample in samples]))


def coverage_within_tolerance(
    observed_coverage: float | None,
    *,
    target_coverage: float = DEFAULT_TARGET_COVERAGE,
    tolerance: float = DEFAULT_COVERAGE_TOLERANCE,
) -> bool:
    """Whether observed coverage is within the allowed tolerance of the target."""
    if observed_coverage is None:
        return False
    return abs(observed_coverage - target_coverage) <= tolerance + _EPSILON


def _distinct_buckets(history: list[dict[str, Any]], cuts: tuple[float, float]) -> set[str]:
    labels: set[str] = set()
    for snapshot in history:
        labels.add(assign_bucket(snapshot, cuts))
    return labels


def _marginal_multiplier(
    samples: list[dict[str, Any]], target_coverage: float, min_samples: int
) -> tuple[float, str]:
    """Marginal (all-bucket) conformal adjustment, mirroring marginal calibration."""
    count = len(samples)
    if count >= min_samples:
        return _conformal_multiplier(samples, target_coverage), "conformal"
    if count >= 10:
        return _legacy_multiplier(samples, target_coverage)[0], "legacy_fallback"
    return 1.0, "legacy_fallback"


def _average_width_pct(samples: list[dict[str, Any]]) -> float | None:
    values = [float(sample["width_pct"]) for sample in samples if "width_pct" in sample]
    return float(np.mean(values)) if values else None


def _bucket_entry(
    name: str,
    samples: list[dict[str, Any]],
    *,
    marginal_multiplier: float,
    target_coverage: float,
    min_samples: int,
    tolerance: float,
) -> dict[str, Any]:
    regime, _, volatility_bucket = name.partition(":")
    count = len(samples)
    if count == 0:
        multiplier: float = marginal_multiplier
        mode = "marginal_fallback"
        shrinkage = 1.0
        raw_multiplier: float | None = None
    else:
        raw_multiplier = _conformal_multiplier(samples, target_coverage)
        if count >= min_samples:
            multiplier = raw_multiplier
            mode = "conformal"
            shrinkage = 0.0
        else:
            weight = count / min_samples
            multiplier = weight * raw_multiplier + (1.0 - weight) * marginal_multiplier
            mode = "shrunken"
            shrinkage = 1.0 - weight

    coverage_after = empirical_coverage(samples, multiplier)
    within = coverage_within_tolerance(
        coverage_after, target_coverage=target_coverage, tolerance=tolerance
    )
    width_before = _average_width_pct(samples)

    return {
        "bucket": name,
        "regime": regime,
        "volatility_bucket": volatility_bucket,
        "samples": count,
        "mode": mode,
        "shrinkage": round(shrinkage, 4),
        "multiplier": round(multiplier, 4),
        "bucket_multiplier": (round(raw_multiplier, 4) if raw_multiplier is not None else None),
        "marginal_multiplier": round(marginal_multiplier, 4),
        "target_coverage": target_coverage,
        "coverage_tolerance": tolerance,
        "coverage_before": _round(empirical_coverage(samples, 1.0)),
        "coverage_after": _round(coverage_after),
        "verified": count >= min_samples and within,
        "within_tolerance": within,
        "average_interval_width_pct_before": _round(width_before),
        "average_interval_width_pct_after": (
            _round(width_before * multiplier) if width_before is not None else None
        ),
    }


def _select_current_bucket(
    history: list[dict[str, Any]],
    regime: str | None,
    market_features: dict[str, Any] | None,
    cuts: tuple[float, float],
) -> str:
    if regime is not None:
        return assign_bucket({"regime": regime, "market_features": market_features or {}}, cuts)
    for snapshot in reversed(history):
        if snapshot.get("regime") is None:
            continue
        return assign_bucket(snapshot, cuts)
    return assign_bucket({"regime": "unknown", "market_features": market_features or {}}, cuts)


def bucket_calibration_details(
    history: list[dict[str, Any]],
    actual_by_timestamp: dict[int, float],
    hour: int,
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
    """Per-bucket conformal recalibration for one horizon with marginal shrinkage.

    Returns the marginal adjustment (the "today" behavior that remains the
    shrinkage target), a report for every bucket observed in its own matured
    history plus the current forecast's bucket, and the multiplier that should
    be applied to the current forecast (full conformal for buckets with >=
    ``min_samples`` matured samples, shrunk toward marginal otherwise).
    """
    _validate_config(target_coverage, history_limit, min_samples, tolerance, cuts)
    marginal_samples = collect_bucket_samples(
        history,
        actual_by_timestamp,
        hour,
        history_limit=history_limit,
        cuts=cuts,
        now=now,
    )
    marginal_multiplier, marginal_mode = _marginal_multiplier(
        marginal_samples, target_coverage, min_samples
    )
    selected_bucket = _select_current_bucket(history, regime, market_features, cuts)

    labels = _distinct_buckets(history, cuts) | {selected_bucket}
    bucket_entries: dict[str, Any] = {}
    for name in sorted(labels):
        samples = collect_bucket_samples(
            history,
            actual_by_timestamp,
            hour,
            bucket=name,
            history_limit=history_limit,
            cuts=cuts,
            now=now,
        )
        bucket_entries[name] = _bucket_entry(
            name,
            samples,
            marginal_multiplier=marginal_multiplier,
            target_coverage=target_coverage,
            min_samples=min_samples,
            tolerance=tolerance,
        )

    selected = dict(bucket_entries[selected_bucket])
    aggregate_coverage = (
        float(
            np.mean(
                [
                    sample["score"] <= float(bucket_entries[sample["bucket"]]["multiplier"])
                    for sample in marginal_samples
                ]
            )
        )
        if marginal_samples
        else None
    )
    aggregate_within_tolerance = coverage_within_tolerance(
        aggregate_coverage,
        target_coverage=target_coverage,
        tolerance=DEFAULT_AGGREGATE_COVERAGE_TOLERANCE,
    )
    fallback_reasons: list[str] = []
    if selected["mode"] in {"shrunken", "marginal_fallback"}:
        fallback_reasons.append("low_segment_evidence")
    if not bool(selected["within_tolerance"]):
        fallback_reasons.append("selected_segment_coverage_unverified")
    if not aggregate_within_tolerance:
        fallback_reasons.append("aggregate_coverage_tolerance")
    if fallback_reasons:
        selected["applied_multiplier"] = round(marginal_multiplier, 4)
        width_before = _finite_float(selected.get("average_interval_width_pct_before"))
        selected["average_interval_width_pct_after"] = (
            _round(width_before * marginal_multiplier) if width_before is not None else None
        )
        selected["decision"] = "marginal_fallback"
        selected["fallback_reasons"] = fallback_reasons
    else:
        selected["applied_multiplier"] = selected["multiplier"]
        selected["decision"] = "validated_segment"
        selected["fallback_reasons"] = []

    violations = sorted(
        name
        for name, entry in bucket_entries.items()
        if int(entry["samples"]) >= min_samples and not bool(entry["within_tolerance"])
    )
    return {
        "horizon": f"{hour}h",
        "target_coverage": target_coverage,
        "history_limit": history_limit,
        "min_samples": min_samples,
        "coverage_tolerance": tolerance,
        "vol_bucket_cuts": [float(cuts[0]), float(cuts[1])],
        "selected_bucket": selected_bucket,
        "selected_volatility_pct": realized_volatility_pct(
            {"regime": regime, "market_features": market_features or {}}
        ),
        "marginal": {
            "samples": len(marginal_samples),
            "mode": marginal_mode,
            "multiplier": round(marginal_multiplier, 4),
            "coverage_after": _round(empirical_coverage(marginal_samples, marginal_multiplier)),
            "average_interval_width_pct": _round(_average_width_pct(marginal_samples)),
        },
        "aggregate_guard": {
            "coverage_after": _round(aggregate_coverage),
            "coverage_tolerance": DEFAULT_AGGREGATE_COVERAGE_TOLERANCE,
            "within_tolerance": aggregate_within_tolerance,
            "protected": True,
        },
        "selected": selected,
        "buckets": bucket_entries,
        "coverage_violations": {
            "bucket_count": len(violations),
            "buckets": violations,
        },
    }


def _recalibrated_interval(
    details: dict[str, Any], prediction: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Rescale the current forecast's interval by the conditional multiplier."""
    if not isinstance(prediction, dict):
        return None
    try:
        price = float(prediction["price_usd"])
        q10 = float(prediction["q10_usd"])
        q90 = float(prediction["q90_usd"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (price, q10, q90)) or price <= 0:
        return None
    conditional = float(details["selected"]["applied_multiplier"])
    engine_multiplier = _finite_float(prediction.get("interval_calibration_multiplier"))
    marginal_multiplier = max(float(details["marginal"]["multiplier"]), _EPSILON)
    if engine_multiplier is not None and engine_multiplier > 0.0:
        base_half_width = (q90 - q10) / 2.0 / engine_multiplier
    else:
        base_half_width = (q90 - q10) / 2.0 / marginal_multiplier
    half_width = base_half_width * conditional
    return {
        "price_usd": round(price, 2),
        "q10_usd": round(max(0.01, price - half_width), 2),
        "q50_usd": round(price, 2),
        "q90_usd": round(price + half_width, 2),
        "half_width_usd": round(half_width, 2),
        "multiplier": round(conditional, 4),
    }


def _coverage_width_report(horizons: dict[str, dict[str, Any]]) -> dict[str, Any]:
    per_horizon: dict[str, Any] = {}
    aggregate_coverage: list[float] = []
    aggregate_width: list[float] = []
    fallback_horizons: list[str] = []
    for horizon, details in horizons.items():
        selected = details["selected"]
        coverage = _finite_float(selected.get("coverage_after"))
        width = _finite_float(selected.get("average_interval_width_pct_after"))
        if coverage is not None:
            aggregate_coverage.append(coverage)
        if width is not None:
            aggregate_width.append(width)
        if selected["decision"] != "validated_segment":
            fallback_horizons.append(horizon)
        per_horizon[horizon] = {
            "segment": details["selected_bucket"],
            "samples": selected["samples"],
            "coverage": coverage,
            "target_coverage": details["target_coverage"],
            "interval_width_pct": width,
            "decision": selected["decision"],
            "fallback_reasons": selected["fallback_reasons"],
            "aggregate_tolerance_protected": details["aggregate_guard"]["within_tolerance"],
        }
    return {
        "per_horizon": per_horizon,
        "aggregate": {
            "mean_segment_coverage": _round(float(np.mean(aggregate_coverage)))
            if aggregate_coverage
            else None,
            "mean_interval_width_pct": _round(float(np.mean(aggregate_width)))
            if aggregate_width
            else None,
            "fallback_horizons": fallback_horizons,
            "all_horizons_validated": not fallback_horizons,
        },
    }


def build_conditional_calibration_section(
    history: list[dict[str, Any]],
    actual_by_timestamp: dict[int, float],
    *,
    regime: str | None = None,
    market_features: dict[str, Any] | None = None,
    predictions: dict[str, Any] | None = None,
    horizons: Iterable[int] = DEFAULT_HORIZONS,
    target_coverage: float = DEFAULT_TARGET_COVERAGE,
    history_limit: int = DEFAULT_HISTORY_LIMIT,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    tolerance: float = DEFAULT_COVERAGE_TOLERANCE,
    cuts: tuple[float, float] = DEFAULT_VOL_BUCKET_CUTS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the JSON-serializable ``conditional_calibration`` forecast section.

    Exports per-horizon bucket reports, the conditional multiplier selected for
    the current bucket, and recalibrated q10/q90 intervals derived by scaling
    the current forecast's interval by the conditional vs marginal multiplier.
    """
    _validate_config(target_coverage, history_limit, min_samples, tolerance, cuts)
    generated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    prediction_map = predictions if isinstance(predictions, dict) else {}
    horizon_results: dict[str, Any] = {}
    verified_horizons = 0
    sparse_horizons = 0
    total_violations = 0
    for hour in horizons:
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
        key = f"{hour}h"
        interval = _recalibrated_interval(details, prediction_map.get(key))
        entry = {
            **details,
            "recalibrated_interval": interval,
            "base_interval": (
                {
                    "price_usd": _round(
                        _finite_float(prediction_map.get(key, {}).get("price_usd"))
                    ),
                    "q10_usd": _round(_finite_float(prediction_map.get(key, {}).get("q10_usd"))),
                    "q50_usd": _round(_finite_float(prediction_map.get(key, {}).get("q50_usd"))),
                    "q90_usd": _round(_finite_float(prediction_map.get(key, {}).get("q90_usd"))),
                }
                if isinstance(prediction_map.get(key), dict)
                else None
            ),
        }
        if bool(entry["selected"]["verified"]):
            verified_horizons += 1
        if str(entry["selected"]["mode"]) in {"shrunken", "marginal_fallback"}:
            sparse_horizons += 1
        total_violations += int(entry["coverage_violations"]["bucket_count"])
        horizon_results[key] = entry

    predictions_present = 0
    for key in horizon_results:
        if horizon_results[key]["base_interval"] is not None:
            predictions_present += 1
    coverage_width_report = _coverage_width_report(horizon_results)
    manifest_payload = {
        "version": CONDITIONAL_CALIBRATION_VERSION,
        "horizons": list(horizon_results),
        "target_coverage": target_coverage,
        "history_limit": history_limit,
        "min_samples": min_samples,
        "coverage_tolerance": tolerance,
        "aggregate_coverage_tolerance": DEFAULT_AGGREGATE_COVERAGE_TOLERANCE,
        "vol_bucket_cuts": [float(cuts[0]), float(cuts[1])],
        "segment_facilities": list(DEFAULT_SEGMENT_DIMENSIONS),
    }
    return {
        "version": CONDITIONAL_CALIBRATION_VERSION,
        "manifest": {
            **manifest_payload,
            "id": _manifest_id(manifest_payload),
            "generated_at": generated_at.isoformat(),
        },
        "meaning": (
            "regime- and realized-volatility-conditional 80% interval calibration: "
            "per-bucket conformal multipliers with shrinkage toward the marginal "
            "adjustment for sparse buckets"
        ),
        "generated_at": generated_at.isoformat(),
        "bucket_dimension": "regime + short-window realized volatility of the dollar move",
        "volatility_feature_keys": list(DEFAULT_VOLATILITY_FEATURE_KEYS),
        "target_coverage": target_coverage,
        "history_limit": history_limit,
        "min_samples": min_samples,
        "coverage_tolerance": tolerance,
        "vol_bucket_cuts": [float(cuts[0]), float(cuts[1])],
        "horizons": horizon_results,
        "overall": {
            "horizon_count": len(horizon_results),
            "verified_horizons": verified_horizons,
            "sparse_horizons": sparse_horizons,
            "coverage_violation_buckets": total_violations,
            "prediction_horizons_with_base_interval": predictions_present,
            "aggregate_coverage_tolerance": DEFAULT_AGGREGATE_COVERAGE_TOLERANCE,
        },
        "coverage_width_report": coverage_width_report,
        "fallback_policy": {
            "mode": "conservative_marginal",
            "triggers": [
                "low_segment_evidence",
                "selected_segment_coverage_unverified",
                "aggregate_coverage_tolerance",
            ],
            "rule": "use the marginal multiplier unless segment and aggregate coverage are healthy",
        },
        "leakage_guard": {
            "source": "matured_snapshot_history",
            "origin_time_only": True,
            "bucket_inputs": ["regime", "market_features." + DEFAULT_VOLATILITY_FEATURE_KEYS[0]],
            "outcome_inputs": ["actual_target_price_usd", "target_at"],
            "rule": (
                "bucket labels are derived only from origin-time snapshot fields; "
                "matured outcomes enter calibration exclusively as normalized "
                "residual scores of forecasts that predate the origin"
            ),
            "reassignment": "origin_time_only",
        },
    }
