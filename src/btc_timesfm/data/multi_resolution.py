#!/usr/bin/env python3
"""Leakage-safe lower-timeframe market-context summaries for BTC/USD.

Production forecasts run on hourly candles, which average away short-term moves.
This module derives compact 5m/15m summary features from aligned high-frequency
candles so research can test whether lower-timeframe context improves 2h/4h
forecasts. Only candles whose close time is at or before the forecast origin
may participate, and no raw high-frequency history is fed to any model.

Aggregates are a pure function of the recipe and the aligned candles, so the
same input always yields the same features and version. Missing lower-timeframe
data degrades silently: ``summarize_if_available`` returns an availability flag
instead of raising, which lets production proceed on hourly-only features.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

import numpy as np

FIVE_MINUTE_SECONDS = 300
FIFTEEN_MINUTE_SECONDS = 900
SCHEMA_VERSION = 1
MIN_ALIGNED_CANDLES = 4

MULTI_RESOLUTION_FEATURE_NAMES = (
    "mr_momentum_1h_pct",
    "mr_momentum_4h_pct",
    "mr_vol_5m_1h_pct",
    "mr_vol_5m_4h_pct",
    "mr_vol_5m_24h_pct",
    "mr_vol_15m_4h_pct",
    "mr_vol_15m_24h_pct",
    "mr_range_1h_avg_pct",
    "mr_range_24h_avg_pct",
    "mr_volume_zscore_1h",
    "mr_volume_zscore_24h",
    "mr_volume_slope_1h",
    "mr_close_position_1h_avg",
    "mr_close_position_24h_avg",
    "mr_close_position_24h_sd",
)

AGGREGATION_RECIPE: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "candle_interval_seconds": FIVE_MINUTE_SECONDS,
    "downsample_15m_bars": FIFTEEN_MINUTE_SECONDS // FIVE_MINUTE_SECONDS,
    "momentum_1h_bars": 12,
    "momentum_4h_bars": 48,
    "realized_volatility_5m_bars": {"1h": 12, "4h": 48, "24h": 288},
    "realized_volatility_15m_bars": {"4h": 16, "24h": 96},
    "range_bars": {"1h": 12, "24h": 288},
    "volume_zscore_bars": {"1h": 12, "24h": 288},
    "volume_slope_1h_bars": 12,
    "close_position_bars": {"1h": 12, "24h": 288},
}


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def feature_set_version(
    *,
    recipe: Mapping[str, Any] | None = None,
    feature_names: tuple[str, ...] | list[str] | None = None,
) -> str:
    """Return a reproducible feature-set version for the aggregation recipe."""
    payload = {
        "recipe": dict(recipe) if recipe is not None else AGGREGATION_RECIPE,
        "feature_names": (
            list(feature_names)
            if feature_names is not None
            else list(MULTI_RESOLUTION_FEATURE_NAMES)
        ),
    }
    digest = _sha256_text(_canonical_json(payload))
    return f"feature-set-{digest[:16]}"


def recipe_sha256(*, recipe: Mapping[str, Any] | None = None) -> str:
    """Return the full sha256 digest of the versioning payload for provenance."""
    payload = {
        "recipe": dict(recipe) if recipe is not None else AGGREGATION_RECIPE,
        "feature_names": list(MULTI_RESOLUTION_FEATURE_NAMES),
    }
    return _sha256_text(_canonical_json(payload))


def _safe_std(values: np.ndarray) -> float:
    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    if values.size < 2:
        return 0.0
    return float(np.std(values, ddof=1))


def _finite_mean(values: np.ndarray) -> float:
    values = np.asarray(values, dtype=np.float64)
    values = values[np.isfinite(values)]
    return float(np.mean(values)) if values.size else 0.0


def _percent_return(closes: np.ndarray, bars: int) -> float:
    closes = np.asarray(closes, dtype=np.float64)
    if closes.size <= bars:
        return 0.0
    previous = float(closes[-1 - bars])
    if previous <= 0 or not np.isfinite(previous):
        return 0.0
    value = float(closes[-1]) / previous - 1.0
    return value * 100.0 if np.isfinite(value) else 0.0


def _realized_volatility(returns: np.ndarray, bars: int) -> float:
    return _safe_std(returns[-bars:]) * 100.0


def _range_pct(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> np.ndarray:
    span = np.asarray(highs, dtype=np.float64) - np.asarray(lows, dtype=np.float64)
    scale = np.asarray(closes, dtype=np.float64)
    result = np.zeros_like(scale)
    np.divide(span, scale, out=result, where=scale > 1e-12)
    result *= 100.0
    return result


def _volume_slope_log_per_hour(log_volume: np.ndarray, bars: int) -> float:
    window = log_volume[-bars:]
    if window.size < 2:
        return 0.0
    x: np.ndarray = np.arange(window.size, dtype=np.float64)
    y = window[np.isfinite(window)]
    if y.size < 2:
        return 0.0
    y = y[-bars:]
    x = x[-y.size :] if y.size < x.size else x
    variance = float(np.var(x))
    base = float(np.mean(y))
    if variance <= 1e-12 or base <= 1e-12:
        return 0.0
    slope = float(np.cov(x, y, ddof=0)[0, 1] / variance)
    per_hour = slope * bars / base
    return per_hour if np.isfinite(per_hour) else 0.0


def _close_positions(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> np.ndarray:
    highs = np.asarray(highs, dtype=np.float64)
    lows = np.asarray(lows, dtype=np.float64)
    closes = np.asarray(closes, dtype=np.float64)
    span = highs - lows
    positions = np.full(closes.shape, 0.5, dtype=np.float64)
    valid = span > 1e-12
    if np.any(valid):
        positions[valid] = (closes[valid] - lows[valid]) / span[valid]
    return np.clip(positions, 0.0, 1.0)


def _volume_zscore(log_volume: np.ndarray, bars: int) -> float:
    window = log_volume[-bars:]
    last = float(window[-1]) if window.size else 0.0
    std = _safe_std(window)
    if std <= 1e-12:
        return 0.0
    return (last - _finite_mean(window)) / std


def _15m_closes(closes: np.ndarray, downsample: int) -> np.ndarray:
    step = max(int(downsample), 1)
    return np.asarray(closes, dtype=np.float64)[-1::-step][::-1]


def _recipe_int(recipe: Mapping[str, Any] | None, key: str, default: int) -> int:
    if recipe is None:
        return default
    value = recipe.get(key, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _recipe_bars(recipe: Mapping[str, Any] | None, key: str, label: str, default: int) -> int:
    if recipe is None:
        return default
    mapping = recipe.get(key, {})
    if not isinstance(mapping, Mapping):
        return default
    value = mapping.get(label, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def derive_multi_resolution_features(
    timestamps: np.ndarray,
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    volumes: np.ndarray,
    origin_ts: int,
    *,
    recipe: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Derive lower-timeframe summaries using only candles at or before origin.

    Any candle whose timestamp is strictly after ``origin_ts`` is excluded before
    any feature is computed, so results never depend on future information. Window
    sizes are read from ``recipe`` (falling back to the module defaults), so the
    sha256 version always describes the aggregation that produced the features.
    """
    momentum_1h_bars = _recipe_int(recipe, "momentum_1h_bars", 12)
    momentum_4h_bars = _recipe_int(recipe, "momentum_4h_bars", 48)
    vol_5m_1h = _recipe_bars(recipe, "realized_volatility_5m_bars", "1h", 12)
    vol_5m_4h = _recipe_bars(recipe, "realized_volatility_5m_bars", "4h", 48)
    vol_5m_24h = _recipe_bars(recipe, "realized_volatility_5m_bars", "24h", 288)
    vol_15m_4h = _recipe_bars(recipe, "realized_volatility_15m_bars", "4h", 16)
    vol_15m_24h = _recipe_bars(recipe, "realized_volatility_15m_bars", "24h", 96)
    range_1h = _recipe_bars(recipe, "range_bars", "1h", 12)
    range_24h = _recipe_bars(recipe, "range_bars", "24h", 288)
    volume_z_1h = _recipe_bars(recipe, "volume_zscore_bars", "1h", 12)
    volume_z_24h = _recipe_bars(recipe, "volume_zscore_bars", "24h", 288)
    volume_slope_1h = _recipe_int(recipe, "volume_slope_1h_bars", 12)
    close_position_1h = _recipe_bars(recipe, "close_position_bars", "1h", 12)
    close_position_24h = _recipe_bars(recipe, "close_position_bars", "24h", 288)
    downsample = _recipe_int(recipe, "downsample_15m_bars", 3)

    times = np.asarray(timestamps)
    arrays = {
        "opens": np.asarray(opens, dtype=np.float64),
        "highs": np.asarray(highs, dtype=np.float64),
        "lows": np.asarray(lows, dtype=np.float64),
        "closes": np.asarray(closes, dtype=np.float64),
        "volumes": np.asarray(volumes, dtype=np.float64),
    }
    for name, array in arrays.items():
        if array.ndim != 1 or array.shape[0] != times.shape[0]:
            raise ValueError(f"high-frequency candles {name!r} must match timestamps shape")
    if times.ndim != 1:
        raise ValueError("high-frequency timestamps must be one-dimensional")

    aligned_indices = np.flatnonzero(times <= origin_ts)
    close = arrays["closes"][aligned_indices]
    high = arrays["highs"][aligned_indices]
    low = arrays["lows"][aligned_indices]
    volume = arrays["volumes"][aligned_indices]
    excluded = int(times.shape[0]) - aligned_indices.size

    if close.size < 2:
        features = {name: 0.0 for name in MULTI_RESOLUTION_FEATURE_NAMES}
    else:
        with np.errstate(divide="ignore", invalid="ignore"):
            returns_5m = np.diff(np.log(close))
        closes_15m = _15m_closes(close, downsample)
        with np.errstate(divide="ignore", invalid="ignore"):
            returns_15m = np.diff(np.log(closes_15m))
        log_volume = np.log1p(np.maximum(volume, 0.0))
        range_pct = _range_pct(high, low, close)
        positions = _close_positions(high, low, close)

        features = {
            "mr_momentum_1h_pct": _percent_return(close, momentum_1h_bars),
            "mr_momentum_4h_pct": _percent_return(close, momentum_4h_bars),
            "mr_vol_5m_1h_pct": _realized_volatility(returns_5m, vol_5m_1h),
            "mr_vol_5m_4h_pct": _realized_volatility(returns_5m, vol_5m_4h),
            "mr_vol_5m_24h_pct": _realized_volatility(returns_5m, vol_5m_24h),
            "mr_vol_15m_4h_pct": _realized_volatility(returns_15m, vol_15m_4h),
            "mr_vol_15m_24h_pct": _realized_volatility(returns_15m, vol_15m_24h),
            "mr_range_1h_avg_pct": _finite_mean(range_pct[-range_1h:]),
            "mr_range_24h_avg_pct": _finite_mean(range_pct[-range_24h:]),
            "mr_volume_zscore_1h": _volume_zscore(log_volume, volume_z_1h),
            "mr_volume_zscore_24h": _volume_zscore(log_volume, volume_z_24h),
            "mr_volume_slope_1h": _volume_slope_log_per_hour(log_volume, volume_slope_1h),
            "mr_close_position_1h_avg": _finite_mean(positions[-close_position_1h:]),
            "mr_close_position_24h_avg": _finite_mean(positions[-close_position_24h:]),
            "mr_close_position_24h_sd": _safe_std(positions[-close_position_24h:]),
        }

    features = {
        name: (round(float(value), 6) if np.isfinite(value) else 0.0)
        for name, value in features.items()
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "available": bool(aligned_indices.size >= MIN_ALIGNED_CANDLES),
        "features": features,
        "feature_names": list(MULTI_RESOLUTION_FEATURE_NAMES),
        "feature_set_version": feature_set_version(recipe=recipe),
        "recipe_sha256": recipe_sha256(recipe=recipe),
        "origin_ts": int(origin_ts),
        "aligned_candles": int(aligned_indices.size),
        "excluded_candles_after_origin": excluded,
    }


def _extract_candle_arrays(high_freq: Any) -> tuple[np.ndarray, ...] | None:
    keys = ("timestamps", "opens", "highs", "lows", "closes", "volumes")
    if isinstance(high_freq, Mapping) and all(key in high_freq for key in keys):
        return tuple(np.asarray(high_freq[key]) for key in keys)
    if all(hasattr(high_freq, key) for key in keys):
        return tuple(getattr(high_freq, key) for key in keys)
    return None


def summarize_if_available(
    high_freq: Any,
    origin_ts: int,
    *,
    recipe: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return lower-timeframe summaries, degrading silently when data is missing."""
    unavailable: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "available": False,
        "features": None,
        "feature_names": list(MULTI_RESOLUTION_FEATURE_NAMES),
        "feature_set_version": None,
        "recipe_sha256": None,
        "origin_ts": int(origin_ts),
        "aligned_candles": 0,
        "excluded_candles_after_origin": 0,
    }
    if high_freq is None:
        return {**unavailable, "reason": "no_data"}
    arrays = _extract_candle_arrays(high_freq)
    if arrays is None:
        return {**unavailable, "reason": "missing_fields"}
    try:
        return derive_multi_resolution_features(
            arrays[0],
            arrays[1],
            arrays[2],
            arrays[3],
            arrays[4],
            arrays[5],
            origin_ts,
            recipe=recipe,
        )
    except (TypeError, ValueError) as exc:
        return {**unavailable, "reason": f"malformed:{exc}"}
