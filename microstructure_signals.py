#!/usr/bin/env python3
"""Timestamp-safe BTC/USD order-book and microstructure signals.

Production uses one bounded public depth request. Kraken is preferred and
Bitstamp is a fallback. Provider failures are isolated so missing order-book
data never blocks the spot forecast.

The order book is observed after the latest completed hourly candle. We retain
both the candle origin and the actual capture timestamp and reject snapshots
captured too far after the origin. Research must use the persisted capture-time
features and matured outcomes rather than reconstructing historical books.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

import requests

KRAKEN_DEPTH_URL = "https://api.kraken.com/0/public/Depth"
BITSTAMP_DEPTH_URL = "https://www.bitstamp.net/api/v2/order_book/btcusd/"
KRAKEN_PAIR = "XBTUSD"
BITSTAMP_PAIR = "btcusd"
SIGNAL_SCHEMA_VERSION = 1
DEFAULT_MAX_ORIGIN_LAG_HOURS = 1.25
DEPTH_BANDS_BPS = (10.0, 25.0)
MICROSTRUCTURE_FEATURE_NAMES = (
    "microstructure_spread_bps",
    "microstructure_bid_depth_usd_10bps",
    "microstructure_ask_depth_usd_10bps",
    "microstructure_imbalance_10bps",
    "microstructure_bid_depth_usd_25bps",
    "microstructure_ask_depth_usd_25bps",
    "microstructure_imbalance_25bps",
    "microstructure_microprice_deviation_bps",
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
        parsed = float(value)
    except ValueError:
        return None
    if not (-float("inf") < parsed < float("inf")):
        return None
    return parsed


def _normalize_levels(rows: Iterable[object], *, bids: bool) -> list[tuple[float, float]]:
    levels: list[tuple[float, float]] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        price = _finite_float(row[0])
        amount = _finite_float(row[1])
        if price is None or amount is None or price <= 0 or amount <= 0:
            continue
        levels.append((price, amount))
    levels.sort(key=lambda item: item[0], reverse=bids)
    return levels


def _depth_usd(
    levels: list[tuple[float, float]], mid: float, band_bps: float, *, bids: bool
) -> float:
    if bids:
        threshold = mid * (1.0 - band_bps / 10_000.0)
        selected = (level for level in levels if level[0] >= threshold)
    else:
        threshold = mid * (1.0 + band_bps / 10_000.0)
        selected = (level for level in levels if level[0] <= threshold)
    return sum(price * amount for price, amount in selected)


def _imbalance(bid_depth: float, ask_depth: float) -> float:
    total = bid_depth + ask_depth
    return (bid_depth - ask_depth) / total if total > 0 else 0.0


def snapshot_from_book(
    origin_at: datetime,
    captured_at: datetime,
    bids: Iterable[object],
    asks: Iterable[object],
    *,
    provider: str,
    pair: str,
    provider_error: str | None = None,
    max_origin_lag_hours: float = DEFAULT_MAX_ORIGIN_LAG_HOURS,
) -> dict[str, Any]:
    """Normalize one L2 snapshot and derive bounded, price-scaled features."""
    origin = _as_utc(origin_at)
    captured = _as_utc(captured_at)
    lag_hours = (captured - origin).total_seconds() / 3600.0
    bid_levels = _normalize_levels(bids, bids=True)
    ask_levels = _normalize_levels(asks, bids=False)

    features: dict[str, float] = {}
    errors: list[str] = []
    if lag_hours < 0:
        errors.append("captured_before_origin")
    if lag_hours > max_origin_lag_hours:
        errors.append("capture_too_late")
    if not bid_levels or not ask_levels:
        errors.append("empty_book")

    if not errors:
        best_bid, best_bid_amount = bid_levels[0]
        best_ask, best_ask_amount = ask_levels[0]
        if best_bid >= best_ask:
            errors.append("crossed_book")
        else:
            mid = (best_bid + best_ask) / 2.0
            spread_bps = (best_ask - best_bid) / mid * 10_000.0
            microprice = (best_ask * best_bid_amount + best_bid * best_ask_amount) / (
                best_bid_amount + best_ask_amount
            )
            features["microstructure_spread_bps"] = spread_bps
            features["microstructure_microprice_deviation_bps"] = (
                (microprice - mid) / mid * 10_000.0
            )
            for band in DEPTH_BANDS_BPS:
                suffix = f"{int(band)}bps"
                bid_depth = _depth_usd(bid_levels, mid, band, bids=True)
                ask_depth = _depth_usd(ask_levels, mid, band, bids=False)
                features[f"microstructure_bid_depth_usd_{suffix}"] = bid_depth
                features[f"microstructure_ask_depth_usd_{suffix}"] = ask_depth
                features[f"microstructure_imbalance_{suffix}"] = _imbalance(bid_depth, ask_depth)

    if provider_error:
        errors.append(provider_error)

    expected = set(MICROSTRUCTURE_FEATURE_NAMES)
    missing = sorted(expected - set(features))
    if not features:
        status = "unavailable"
    elif errors or missing:
        status = "partial"
    else:
        status = "ok"

    return {
        "schema_version": SIGNAL_SCHEMA_VERSION,
        "origin_at": origin.isoformat(),
        "captured_at": captured.isoformat(),
        "status": status,
        "available": bool(features),
        "features": {name: round(value, 8) for name, value in sorted(features.items())},
        "quality": {
            "capture_lag_hours": round(lag_hours, 6),
            "max_origin_lag_hours": max_origin_lag_hours,
            "bid_levels": len(bid_levels),
            "ask_levels": len(ask_levels),
            "missing_features": missing,
            "errors": errors,
        },
        "provider": {"name": provider, "pair": pair},
    }


def _kraken_book(payload: object) -> tuple[list[object], list[object]]:
    if not isinstance(payload, dict):
        raise ValueError("unexpected Kraken response")
    errors = payload.get("error")
    if isinstance(errors, list) and errors:
        raise ValueError("Kraken API returned an error")
    result = payload.get("result")
    if not isinstance(result, dict) or not result:
        raise ValueError("Kraken response has no order book")
    book = next(iter(result.values()))
    if not isinstance(book, dict):
        raise ValueError("Kraken order book is malformed")
    bids = book.get("bids")
    asks = book.get("asks")
    if not isinstance(bids, list) or not isinstance(asks, list):
        raise ValueError("Kraken order book sides are missing")
    return bids, asks


def _bitstamp_book(payload: object) -> tuple[list[object], list[object]]:
    if not isinstance(payload, dict):
        raise ValueError("unexpected Bitstamp response")
    bids = payload.get("bids")
    asks = payload.get("asks")
    if not isinstance(bids, list) or not isinstance(asks, list):
        raise ValueError("Bitstamp order book sides are missing")
    return bids, asks


def fetch_microstructure_snapshot(
    origin_at: datetime,
    *,
    captured_at: datetime | None = None,
) -> dict[str, Any]:
    """Fetch Kraken depth with Bitstamp fallback; never raise on provider outage."""
    origin = _as_utc(origin_at)
    provider_errors: list[str] = []

    try:
        response = requests.get(
            KRAKEN_DEPTH_URL,
            params=(("pair", KRAKEN_PAIR), ("count", 100)),
            timeout=15,
        )
        response.raise_for_status()
        bids, asks = _kraken_book(response.json())
        captured = _as_utc(captured_at or datetime.now(timezone.utc))
        return snapshot_from_book(
            origin,
            captured,
            bids,
            asks,
            provider="kraken_spot",
            pair=KRAKEN_PAIR,
        )
    except (requests.RequestException, ValueError) as exc:
        provider_errors.append(f"kraken:{type(exc).__name__}")

    try:
        response = requests.get(BITSTAMP_DEPTH_URL, timeout=15)
        response.raise_for_status()
        bids, asks = _bitstamp_book(response.json())
        captured = _as_utc(captured_at or datetime.now(timezone.utc))
        return snapshot_from_book(
            origin,
            captured,
            bids,
            asks,
            provider="bitstamp_spot",
            pair=BITSTAMP_PAIR,
            provider_error=";".join(provider_errors) if provider_errors else None,
        )
    except (requests.RequestException, ValueError) as exc:
        provider_errors.append(f"bitstamp:{type(exc).__name__}")

    captured = _as_utc(captured_at or datetime.now(timezone.utc))
    return snapshot_from_book(
        origin,
        captured,
        [],
        [],
        provider="none",
        pair="BTC/USD",
        provider_error=";".join(provider_errors),
    )


def signal_manifest(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Small provenance payload for immutable experiment manifests."""
    quality_value = snapshot.get("quality")
    quality: dict[str, Any] = quality_value if isinstance(quality_value, dict) else {}
    features_value = snapshot.get("features")
    features: dict[str, Any] = features_value if isinstance(features_value, dict) else {}
    return {
        "schema_version": snapshot.get("schema_version"),
        "status": snapshot.get("status"),
        "provider": snapshot.get("provider", {}),
        "captured_at": snapshot.get("captured_at"),
        "capture_lag_hours": quality.get("capture_lag_hours"),
        "feature_names": sorted(features),
        "missing_features": quality.get("missing_features", []),
        "errors": quality.get("errors", []),
    }
