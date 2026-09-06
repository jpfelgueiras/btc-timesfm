#!/usr/bin/env python3
"""Timestamp-safe cross-asset and macro context for BTC forecasts.

ETH/USD hourly candles provide crypto-beta context. Daily VIX and US 10-year
Treasury observations come from FRED. Daily macro observations are conservatively
considered available only from 00:00 UTC on the following calendar day, so a
same-day value can never leak into a historical forecast origin.

Provider failures are isolated and return quality metadata instead of breaking
production. These features are persisted as research context only; their
presence does not alter production forecasts until out-of-sample evidence
supports promotion.
"""

from __future__ import annotations

import csv
import io
import math
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any, Iterable

import numpy as np
import requests

from forecast_engine import INTERVAL_MINUTES, MarketData

KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
ETH_PAIR = "ETHUSD"
VIX_SERIES = "VIXCLS"
US10Y_SERIES = "DGS10"
SIGNAL_SCHEMA_VERSION = 1
DEFAULT_MAX_ETH_AGE_HOURS = 2.0
DEFAULT_MAX_MACRO_AGE_DAYS = 7.0
CROSS_ASSET_FEATURE_NAMES = (
    "cross_eth_return_1h_pct",
    "cross_eth_return_6h_pct",
    "cross_eth_return_24h_pct",
    "cross_eth_btc_relative_6h_pct",
    "cross_eth_btc_relative_24h_pct",
    "cross_btc_eth_corr_24h",
    "cross_btc_eth_corr_168h",
    "macro_vix_level",
    "macro_vix_change_5d_pct",
    "macro_us10y_yield_pct",
    "macro_us10y_change_5d_bp",
)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _finite_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def fetch_kraken_pair_hourly(pair: str, limit: int = 720) -> MarketData:
    """Fetch recent completed Kraken hourly candles for an arbitrary spot pair."""
    if limit < 2 or limit > 720:
        raise ValueError("Kraken history limit must be between 2 and 720")
    response = requests.get(
        KRAKEN_OHLC_URL,
        params={"pair": pair, "interval": INTERVAL_MINUTES},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("error"):
        raise RuntimeError(f"Kraken API error for {pair}")
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"Kraken response for {pair} has no result")
    pair_key = next((key for key in result if key != "last"), None)
    if pair_key is None or not isinstance(result[pair_key], list):
        raise RuntimeError(f"Kraken response for {pair} has no candles")
    now = time.time()
    completed = [
        candle
        for candle in result[pair_key]
        if isinstance(candle, list)
        and len(candle) >= 7
        and _finite_float(candle[0]) is not None
        and float(candle[0]) + INTERVAL_MINUTES * 60 <= now
    ][-limit:]
    if len(completed) < 2:
        raise RuntimeError(f"Not enough completed Kraken candles for {pair}")

    def arr(index: int) -> np.ndarray:
        return np.asarray([float(candle[index]) for candle in completed], dtype=np.float32)

    return MarketData(
        timestamps=[int(float(candle[0])) + INTERVAL_MINUTES * 60 for candle in completed],
        opens=arr(1),
        highs=arr(2),
        lows=arr(3),
        closes=arr(4),
        volumes=arr(6),
    )


def parse_fred_csv(text: str, series_id: str) -> list[dict[str, Any]]:
    """Parse FRED graph CSV into dated finite observations."""
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict[str, Any]] = []
    for row in reader:
        raw_date = row.get("observation_date") or row.get("DATE")
        if not raw_date:
            continue
        try:
            observed_on = date.fromisoformat(raw_date)
        except ValueError:
            continue
        value = _finite_float(row.get(series_id))
        if value is None:
            continue
        rows.append({"date": observed_on.isoformat(), "value": value})
    return rows


def fetch_fred_series(series_id: str) -> list[dict[str, Any]]:
    response = requests.get(FRED_CSV_URL, params={"id": series_id}, timeout=30)
    response.raise_for_status()
    return parse_fred_csv(response.text, series_id)


def _bounded_close_map(data: MarketData, origin_s: int) -> dict[int, float]:
    return {
        int(timestamp): float(close)
        for timestamp, close in zip(data.timestamps, data.closes, strict=True)
        if int(timestamp) <= origin_s and float(close) > 0
    }


def _pct_change(values: list[float], hours: int) -> float | None:
    if len(values) <= hours or values[-1 - hours] <= 0:
        return None
    return (values[-1] / values[-1 - hours] - 1.0) * 100.0


def _correlation(btc: list[float], eth: list[float], hours: int) -> float | None:
    if len(btc) <= hours or len(eth) <= hours:
        return None
    btc_returns = np.diff(np.log(np.asarray(btc[-(hours + 1) :], dtype=float)))
    eth_returns = np.diff(np.log(np.asarray(eth[-(hours + 1) :], dtype=float)))
    if float(np.std(btc_returns)) < 1e-12 or float(np.std(eth_returns)) < 1e-12:
        return 0.0
    return float(np.corrcoef(btc_returns, eth_returns)[0, 1])


def _macro_available_at(observed_on: date) -> datetime:
    """Conservative availability time: next calendar day at 00:00 UTC."""
    return datetime.combine(observed_on + timedelta(days=1), dt_time.min, tzinfo=timezone.utc)


def _eligible_macro(
    observations: Iterable[dict[str, Any]], origin: datetime
) -> list[tuple[date, float]]:
    eligible: list[tuple[date, float]] = []
    for row in observations:
        raw_date = row.get("date")
        value = _finite_float(row.get("value"))
        if not isinstance(raw_date, str) or value is None:
            continue
        try:
            observed_on = date.fromisoformat(raw_date)
        except ValueError:
            continue
        if _macro_available_at(observed_on) <= origin:
            eligible.append((observed_on, value))
    eligible.sort(key=lambda item: item[0])
    return eligible


def _macro_features(
    prefix: str,
    observations: Iterable[dict[str, Any]],
    origin: datetime,
    *,
    level_name: str,
    change_name: str,
    change_is_basis_points: bool,
    max_age_days: float,
) -> tuple[dict[str, float], dict[str, Any]]:
    eligible = _eligible_macro(observations, origin)
    if not eligible:
        return {}, {"latest_observation_date": None, "age_days": None, "stale": True}
    latest_date, latest = eligible[-1]
    age_days = (origin - _macro_available_at(latest_date)).total_seconds() / 86400.0
    stale = age_days > max_age_days
    features: dict[str, float] = {}
    if not stale:
        features[level_name] = latest
        if len(eligible) >= 6:
            previous = eligible[-6][1]
            if change_is_basis_points:
                features[change_name] = (latest - previous) * 100.0
            elif previous != 0:
                features[change_name] = (latest / previous - 1.0) * 100.0
    return features, {
        "series": prefix,
        "latest_observation_date": latest_date.isoformat(),
        "available_at": _macro_available_at(latest_date).isoformat(),
        "age_days": round(age_days, 4),
        "stale": stale,
    }


def snapshot_from_inputs(
    origin_at: datetime,
    btc_data: MarketData,
    eth_data: MarketData,
    vix_observations: Iterable[dict[str, Any]],
    us10y_observations: Iterable[dict[str, Any]],
    *,
    errors: dict[str, str] | None = None,
    max_eth_age_hours: float = DEFAULT_MAX_ETH_AGE_HOURS,
    max_macro_age_days: float = DEFAULT_MAX_MACRO_AGE_DAYS,
) -> dict[str, Any]:
    """Derive leakage-safe cross-asset features at one forecast origin."""
    origin = _as_utc(origin_at)
    origin_s = int(origin.timestamp())
    btc_map = _bounded_close_map(btc_data, origin_s)
    eth_map = _bounded_close_map(eth_data, origin_s)
    common = sorted(set(btc_map) & set(eth_map))
    features: dict[str, float] = {}
    quality: dict[str, Any] = {}

    latest_common = common[-1] if common else None
    eth_age_hours = (origin_s - latest_common) / 3600.0 if latest_common is not None else None
    eth_stale = eth_age_hours is None or eth_age_hours > max_eth_age_hours
    quality["eth_age_hours"] = round(eth_age_hours, 4) if eth_age_hours is not None else None
    quality["eth_stale"] = eth_stale
    quality["aligned_hourly_candles"] = len(common)

    if common and not eth_stale:
        btc = [btc_map[timestamp] for timestamp in common]
        eth = [eth_map[timestamp] for timestamp in common]
        eth_1h = _pct_change(eth, 1)
        eth_6h = _pct_change(eth, 6)
        eth_24h = _pct_change(eth, 24)
        btc_6h = _pct_change(btc, 6)
        btc_24h = _pct_change(btc, 24)
        if eth_1h is not None:
            features["cross_eth_return_1h_pct"] = eth_1h
        if eth_6h is not None:
            features["cross_eth_return_6h_pct"] = eth_6h
        if eth_24h is not None:
            features["cross_eth_return_24h_pct"] = eth_24h
        if eth_6h is not None and btc_6h is not None:
            features["cross_eth_btc_relative_6h_pct"] = eth_6h - btc_6h
        if eth_24h is not None and btc_24h is not None:
            features["cross_eth_btc_relative_24h_pct"] = eth_24h - btc_24h
        corr24 = _correlation(btc, eth, 24)
        corr168 = _correlation(btc, eth, 168)
        if corr24 is not None:
            features["cross_btc_eth_corr_24h"] = corr24
        if corr168 is not None:
            features["cross_btc_eth_corr_168h"] = corr168

    vix_features, vix_quality = _macro_features(
        VIX_SERIES,
        vix_observations,
        origin,
        level_name="macro_vix_level",
        change_name="macro_vix_change_5d_pct",
        change_is_basis_points=False,
        max_age_days=max_macro_age_days,
    )
    yield_features, yield_quality = _macro_features(
        US10Y_SERIES,
        us10y_observations,
        origin,
        level_name="macro_us10y_yield_pct",
        change_name="macro_us10y_change_5d_bp",
        change_is_basis_points=True,
        max_age_days=max_macro_age_days,
    )
    features.update(vix_features)
    features.update(yield_features)
    quality["vix"] = vix_quality
    quality["us10y"] = yield_quality
    quality["provider_errors"] = dict(errors or {})

    expected = set(CROSS_ASSET_FEATURE_NAMES)
    missing = sorted(expected - set(features))
    quality["missing_features"] = missing
    provider_errors = quality["provider_errors"]
    if not features:
        status = "unavailable"
    elif missing or provider_errors or eth_stale or vix_quality["stale"] or yield_quality["stale"]:
        status = "partial"
    else:
        status = "ok"

    return {
        "schema_version": SIGNAL_SCHEMA_VERSION,
        "origin_at": origin.isoformat(),
        "status": status,
        "available": bool(features),
        "features": {name: round(value, 8) for name, value in sorted(features.items())},
        "quality": quality,
        "providers": {
            "crypto_beta": {"name": "kraken_spot", "pair": ETH_PAIR},
            "vix": {"name": "fred", "series": VIX_SERIES},
            "us10y": {"name": "fred", "series": US10Y_SERIES},
        },
    }


def fetch_cross_asset_snapshot(origin_at: datetime, btc_data: MarketData) -> dict[str, Any]:
    """Fetch production cross-asset context without making it a hard dependency."""
    errors: dict[str, str] = {}
    empty = MarketData(
        timestamps=[],
        opens=np.asarray([], dtype=np.float32),
        highs=np.asarray([], dtype=np.float32),
        lows=np.asarray([], dtype=np.float32),
        closes=np.asarray([], dtype=np.float32),
        volumes=np.asarray([], dtype=np.float32),
    )
    eth_data = empty
    vix: list[dict[str, Any]] = []
    us10y: list[dict[str, Any]] = []
    try:
        eth_data = fetch_kraken_pair_hourly(ETH_PAIR, 720)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        errors["kraken_eth"] = type(exc).__name__
    try:
        vix = fetch_fred_series(VIX_SERIES)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        errors["fred_vix"] = type(exc).__name__
    try:
        us10y = fetch_fred_series(US10Y_SERIES)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        errors["fred_us10y"] = type(exc).__name__
    return snapshot_from_inputs(origin_at, btc_data, eth_data, vix, us10y, errors=errors)


def signal_manifest(snapshot: dict[str, Any]) -> dict[str, Any]:
    quality_value = snapshot.get("quality")
    quality: dict[str, Any] = quality_value if isinstance(quality_value, dict) else {}
    features_value = snapshot.get("features")
    features: dict[str, Any] = features_value if isinstance(features_value, dict) else {}
    return {
        "schema_version": snapshot.get("schema_version"),
        "status": snapshot.get("status"),
        "providers": snapshot.get("providers", {}),
        "feature_names": sorted(features),
        "missing_features": quality.get("missing_features", []),
        "provider_errors": quality.get("provider_errors", {}),
        "eth_age_hours": quality.get("eth_age_hours"),
        "vix": quality.get("vix", {}),
        "us10y": quality.get("us10y", {}),
    }
