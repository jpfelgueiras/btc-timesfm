#!/usr/bin/env python3
"""Timestamp-safe BTC derivatives signals for production and research.

Funding comes from Binance USD-M BTCUSDT perpetual futures. Open interest and
liquidation notionals come from Gate BTC_USDT perpetual contract statistics,
which expose hourly historical aggregates without authentication.

All normalization is origin-time bounded: rows newer than the forecast origin
are ignored before features are derived. Provider failures and stale inputs are
reported as quality metadata instead of breaking the production forecast.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import requests


BINANCE_FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
GATE_CONTRACT_STATS_URL = "https://api.gateio.ws/api/v4/futures/usdt/contract_stats"
BINANCE_SYMBOL = "BTCUSDT"
GATE_CONTRACT = "BTC_USDT"
SIGNAL_SCHEMA_VERSION = 1
DEFAULT_MAX_FUNDING_AGE_HOURS = 12.0
DEFAULT_MAX_STATS_AGE_HOURS = 2.5
DERIVATIVE_FEATURE_NAMES = (
    "derivatives_funding_rate_pct",
    "derivatives_open_interest_usd",
    "derivatives_oi_change_1h_pct",
    "derivatives_oi_change_24h_pct",
    "derivatives_long_liquidation_usd_1h",
    "derivatives_short_liquidation_usd_1h",
    "derivatives_liquidation_total_usd_1h",
    "derivatives_liquidation_imbalance",
)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _timestamp_seconds(row: dict[str, Any]) -> int | None:
    value = row.get("time")
    numeric = _float(value)
    if numeric is None:
        return None
    timestamp = int(numeric)
    if timestamp > 10_000_000_000:
        timestamp //= 1000
    return timestamp


def _funding_timestamp_seconds(row: dict[str, Any]) -> int | None:
    numeric = _float(row.get("fundingTime"))
    if numeric is None:
        return None
    timestamp = int(numeric)
    return timestamp // 1000 if timestamp > 10_000_000_000 else timestamp


def _bounded_rows(
    rows: Iterable[dict[str, Any]],
    origin_s: int,
    *,
    funding: bool = False,
) -> list[dict[str, Any]]:
    timestamp = _funding_timestamp_seconds if funding else _timestamp_seconds
    usable = [row for row in rows if (ts := timestamp(row)) is not None and ts <= origin_s]
    return sorted(usable, key=lambda row: timestamp(row) or 0)


def _nearest_at_or_before(rows: list[dict[str, Any]], target_s: int) -> dict[str, Any] | None:
    eligible = [row for row in rows if (_timestamp_seconds(row) or target_s + 1) <= target_s]
    return eligible[-1] if eligible else None


def _field(row: dict[str, Any] | None, names: tuple[str, ...]) -> float | None:
    if not isinstance(row, dict):
        return None
    for name in names:
        value = _float(row.get(name))
        if value is not None:
            return value
    return None


def _pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous <= 0:
        return None
    return (current / previous - 1.0) * 100.0


def snapshot_from_rows(
    origin_at: datetime,
    funding_rows: Iterable[dict[str, Any]],
    stats_rows: Iterable[dict[str, Any]],
    *,
    errors: dict[str, str] | None = None,
    max_funding_age_hours: float = DEFAULT_MAX_FUNDING_AGE_HOURS,
    max_stats_age_hours: float = DEFAULT_MAX_STATS_AGE_HOURS,
) -> dict[str, Any]:
    """Create a leakage-safe signal snapshot from already-fetched provider rows."""
    origin = _as_utc(origin_at)
    origin_s = int(origin.timestamp())
    funding = _bounded_rows(funding_rows, origin_s, funding=True)
    stats = _bounded_rows(stats_rows, origin_s)
    latest_funding = funding[-1] if funding else None
    latest_stats = stats[-1] if stats else None

    funding_ts = _funding_timestamp_seconds(latest_funding) if latest_funding else None
    stats_ts = _timestamp_seconds(latest_stats) if latest_stats else None
    funding_age = (origin_s - funding_ts) / 3600.0 if funding_ts is not None else None
    stats_age = (origin_s - stats_ts) / 3600.0 if stats_ts is not None else None
    funding_stale = funding_age is None or funding_age > max_funding_age_hours
    stats_stale = stats_age is None or stats_age > max_stats_age_hours

    features: dict[str, float] = {}
    if latest_funding is not None and not funding_stale:
        rate = _float(latest_funding.get("fundingRate"))
        if rate is not None:
            features["derivatives_funding_rate_pct"] = rate * 100.0

    if latest_stats is not None and not stats_stale and stats_ts is not None:
        open_interest = _field(latest_stats, ("open_interest_usd", "open_interest"))
        previous_1h = _nearest_at_or_before(stats, stats_ts - 3600)
        previous_24h = _nearest_at_or_before(stats, stats_ts - 24 * 3600)
        open_interest_1h = _field(previous_1h, ("open_interest_usd", "open_interest"))
        open_interest_24h = _field(previous_24h, ("open_interest_usd", "open_interest"))
        long_liq = _field(latest_stats, ("long_liq_usd_new", "long_liq_usd")) or 0.0
        short_liq = _field(latest_stats, ("short_liq_usd_new", "short_liq_usd")) or 0.0
        total_liq = max(0.0, long_liq) + max(0.0, short_liq)

        if open_interest is not None:
            features["derivatives_open_interest_usd"] = open_interest
        oi_1h = _pct_change(open_interest, open_interest_1h)
        oi_24h = _pct_change(open_interest, open_interest_24h)
        if oi_1h is not None:
            features["derivatives_oi_change_1h_pct"] = oi_1h
        if oi_24h is not None:
            features["derivatives_oi_change_24h_pct"] = oi_24h
        features["derivatives_long_liquidation_usd_1h"] = max(0.0, long_liq)
        features["derivatives_short_liquidation_usd_1h"] = max(0.0, short_liq)
        features["derivatives_liquidation_total_usd_1h"] = total_liq
        features["derivatives_liquidation_imbalance"] = (
            (max(0.0, short_liq) - max(0.0, long_liq)) / total_liq if total_liq > 0 else 0.0
        )

    provider_errors = dict(errors or {})
    stale_sources: list[str] = []
    if funding_stale:
        stale_sources.append("binance_funding")
    if stats_stale:
        stale_sources.append("gate_contract_stats")

    expected = set(DERIVATIVE_FEATURE_NAMES)
    missing_features = sorted(expected - set(features))
    if not features:
        status = "unavailable"
    elif provider_errors or stale_sources or missing_features:
        status = "partial"
    else:
        status = "ok"

    return {
        "schema_version": SIGNAL_SCHEMA_VERSION,
        "origin_at": origin.isoformat(),
        "status": status,
        "available": bool(features),
        "features": {name: round(value, 8) for name, value in sorted(features.items())},
        "quality": {
            "funding_age_hours": round(funding_age, 4) if funding_age is not None else None,
            "stats_age_hours": round(stats_age, 4) if stats_age is not None else None,
            "stale_sources": stale_sources,
            "missing_features": missing_features,
            "provider_errors": provider_errors,
        },
        "providers": {
            "funding": {"name": "binance_usdm", "symbol": BINANCE_SYMBOL},
            "open_interest_liquidations": {"name": "gate_futures", "contract": GATE_CONTRACT},
        },
        "raw": {
            "latest_funding": latest_funding,
            "latest_contract_stats": latest_stats,
        },
    }


def fetch_derivatives_snapshot(origin_at: datetime) -> dict[str, Any]:
    """Fetch the latest origin-safe production signal snapshot.

    Each provider is isolated so an outage never aborts the spot forecast.
    """
    origin = _as_utc(origin_at)
    origin_s = int(origin.timestamp())
    origin_ms = origin_s * 1000
    funding_rows: list[dict[str, Any]] = []
    stats_rows: list[dict[str, Any]] = []
    errors: dict[str, str] = {}

    try:
        response = requests.get(
            BINANCE_FUNDING_URL,
            params={"symbol": BINANCE_SYMBOL, "endTime": origin_ms, "limit": 16},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            funding_rows = [row for row in payload if isinstance(row, dict)]
        else:
            errors["binance_funding"] = "unexpected_response"
    except (requests.RequestException, ValueError) as exc:
        errors["binance_funding"] = type(exc).__name__

    try:
        response = requests.get(
            GATE_CONTRACT_STATS_URL,
            params={
                "contract": GATE_CONTRACT,
                "from": origin_s - 26 * 3600,
                "to": origin_s,
                "interval": "1h",
                "limit": 100,
            },
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            stats_rows = [row for row in payload if isinstance(row, dict)]
        else:
            errors["gate_contract_stats"] = "unexpected_response"
    except (requests.RequestException, ValueError) as exc:
        errors["gate_contract_stats"] = type(exc).__name__

    return snapshot_from_rows(origin, funding_rows, stats_rows, errors=errors)


def fetch_derivatives_history(
    start_at: datetime,
    end_at: datetime,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch bounded historical rows for research-only walk-forward ablation."""
    start = _as_utc(start_at)
    end = _as_utc(end_at)
    if end <= start:
        raise ValueError("end_at must be after start_at")
    if end - start > timedelta(days=31):
        raise ValueError("derivatives research window is capped at 31 days")

    start_s = int(start.timestamp())
    end_s = int(end.timestamp())
    funding_response = requests.get(
        BINANCE_FUNDING_URL,
        params={
            "symbol": BINANCE_SYMBOL,
            "startTime": start_s * 1000,
            "endTime": end_s * 1000,
            "limit": 1000,
        },
        timeout=30,
    )
    funding_response.raise_for_status()
    funding_payload = funding_response.json()

    stats_response = requests.get(
        GATE_CONTRACT_STATS_URL,
        params={
            "contract": GATE_CONTRACT,
            "from": start_s,
            "to": end_s,
            "interval": "1h",
            "limit": 1000,
        },
        timeout=30,
    )
    stats_response.raise_for_status()
    stats_payload = stats_response.json()

    if not isinstance(funding_payload, list) or not isinstance(stats_payload, list):
        raise RuntimeError("Unexpected derivatives history response")
    return {
        "funding": [row for row in funding_payload if isinstance(row, dict)],
        "stats": [row for row in stats_payload if isinstance(row, dict)],
    }


def signal_manifest(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Small provenance payload suitable for reproducible experiment manifests."""
    quality = snapshot.get("quality") if isinstance(snapshot.get("quality"), dict) else {}
    return {
        "schema_version": snapshot.get("schema_version"),
        "status": snapshot.get("status"),
        "providers": snapshot.get("providers", {}),
        "stale_sources": quality.get("stale_sources", []),
        "missing_features": quality.get("missing_features", []),
        "feature_names": sorted(snapshot.get("features", {})),
    }
