"""Redundant hourly BTC market-data providers for production forecasts.

Kraken remains the preferred BTC/USD source. Binance BTCUSDT is a liquid
secondary source used only when the primary is unavailable or fails validation.
When both sources are available their overlapping closes are compared so a
provider-specific anomaly cannot silently enter the forecast pipeline.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import requests

from forecast_engine import CONTEXT_WINDOWS, INTERVAL_MINUTES, MarketData, fetch_kraken_hourly
from market_data_validation import (
    MarketDataValidationError,
    ValidationConfig,
    ValidationReport,
    persist_validation_report,
    validate_market_data,
)


BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
SOURCE_REPORT_PATH = Path("market_data_source.json")


class HourlyMarketDataProvider(Protocol):
    name: str
    pair: str

    def fetch(self, limit: int) -> MarketData: ...


@dataclass(frozen=True)
class ProviderConfig:
    max_close_difference_pct: float = 0.75
    comparison_candles: int = 24
    min_overlap_candles: int = 6

    @classmethod
    def from_env(cls) -> "ProviderConfig":
        return cls(
            max_close_difference_pct=_positive_float(
                "BTC_PROVIDER_MAX_CLOSE_DIFF_PCT", cls.max_close_difference_pct
            ),
            comparison_candles=_positive_int(
                "BTC_PROVIDER_COMPARE_CANDLES", cls.comparison_candles
            ),
            min_overlap_candles=_positive_int(
                "BTC_PROVIDER_MIN_OVERLAP", cls.min_overlap_candles
            ),
        )


@dataclass
class ProviderAttempt:
    name: str
    pair: str
    data: MarketData | None
    validation: ValidationReport | None
    error: str | None

    @property
    def healthy(self) -> bool:
        return self.data is not None and self.validation is not None and self.validation.ok

    def diagnostics(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "pair": self.pair,
            "healthy": self.healthy,
            "error": self.error,
            "validation": self.validation.to_dict() if self.validation is not None else None,
        }


@dataclass
class MarketDataSelection:
    data: MarketData
    provider: str
    source: str
    source_pair: str
    fallback_used: bool
    comparison: dict[str, Any] | None
    primary: ProviderAttempt
    secondary: ProviderAttempt

    def diagnostics(self) -> dict[str, Any]:
        return {
            "selected_provider": self.provider,
            "selected_source": self.source,
            "selected_pair": self.source_pair,
            "fallback_used": self.fallback_used,
            "comparison": self.comparison,
            "primary": self.primary.diagnostics(),
            "secondary": self.secondary.diagnostics(),
        }


class ProviderDisagreementError(RuntimeError):
    pass


class NoHealthyMarketDataProvider(RuntimeError):
    pass


@dataclass(frozen=True)
class KrakenProvider:
    name: str = "kraken"
    pair: str = "BTC/USD"

    def fetch(self, limit: int) -> MarketData:
        return fetch_kraken_hourly(limit)


@dataclass(frozen=True)
class BinanceProvider:
    name: str = "binance"
    pair: str = "BTC/USDT"

    def fetch(self, limit: int) -> MarketData:
        return fetch_binance_hourly(limit)


def _positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return float(default)
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return int(default)
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def fetch_binance_hourly(limit: int = 512) -> MarketData:
    """Fetch recent completed Binance BTCUSDT hourly candles.

    Timestamps are normalized to candle-close UTC seconds, matching Kraken and
    the rest of the project. The currently forming candle is excluded.
    """

    limit = max(limit, max(CONTEXT_WINDOWS) + 1)
    if limit > 1000:
        raise ValueError("Binance recent-kline endpoint supports at most 1000 candles")

    response = requests.get(
        BINANCE_KLINES_URL,
        params={"symbol": "BTCUSDT", "interval": "1h", "limit": limit + 1},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("Unexpected Binance kline response")

    now = time.time()
    completed = [
        row
        for row in payload
        if isinstance(row, list)
        and len(row) >= 7
        and int(row[0]) / 1000 + INTERVAL_MINUTES * 60 <= now
    ][-limit:]
    if len(completed) < 64:
        raise RuntimeError(f"Not enough completed Binance candles: {len(completed)}")

    def arr(index: int) -> np.ndarray:
        return np.asarray([float(row[index]) for row in completed], dtype=np.float32)

    return MarketData(
        timestamps=[int(row[0] / 1000) + INTERVAL_MINUTES * 60 for row in completed],
        opens=arr(1),
        highs=arr(2),
        lows=arr(3),
        closes=arr(4),
        volumes=arr(5),
    )


def _attempt_provider(
    provider: HourlyMarketDataProvider,
    limit: int,
    *,
    now: datetime,
    validation_config: ValidationConfig,
) -> ProviderAttempt:
    try:
        data = provider.fetch(limit)
    except Exception as exc:  # Network/provider failures are failover inputs.
        return ProviderAttempt(provider.name, provider.pair, None, None, f"fetch: {exc}")

    try:
        report = validate_market_data(
            data,
            source=f"{provider.name} {provider.pair} hourly OHLCV",
            now=now,
            config=validation_config,
            check_staleness=True,
        )
    except MarketDataValidationError as exc:
        return ProviderAttempt(provider.name, provider.pair, data, exc.report, str(exc))
    return ProviderAttempt(provider.name, provider.pair, data, report, None)


def compare_overlapping_closes(
    primary: MarketData,
    secondary: MarketData,
    *,
    config: ProviderConfig,
) -> dict[str, Any]:
    primary_closes = dict(zip(primary.timestamps, map(float, primary.closes), strict=True))
    secondary_closes = dict(zip(secondary.timestamps, map(float, secondary.closes), strict=True))
    common = sorted(set(primary_closes).intersection(secondary_closes))[-config.comparison_candles :]
    if len(common) < config.min_overlap_candles:
        return {
            "status": "insufficient_overlap",
            "overlap_candles": len(common),
            "required_overlap_candles": config.min_overlap_candles,
            "max_close_difference_pct": None,
            "mean_close_difference_pct": None,
            "tolerance_pct": config.max_close_difference_pct,
        }

    differences = []
    for timestamp in common:
        left = primary_closes[timestamp]
        right = secondary_closes[timestamp]
        midpoint = (left + right) / 2.0
        difference = abs(left - right) / midpoint * 100.0 if midpoint > 0 else float("inf")
        differences.append(difference)

    maximum = max(differences)
    return {
        "status": "ok" if maximum <= config.max_close_difference_pct else "disagreement",
        "overlap_candles": len(common),
        "first_overlap_at": common[0],
        "latest_overlap_at": common[-1],
        "max_close_difference_pct": round(maximum, 6),
        "mean_close_difference_pct": round(float(np.mean(differences)), 6),
        "tolerance_pct": config.max_close_difference_pct,
    }


def select_market_data(
    limit: int = 512,
    *,
    primary_provider: HourlyMarketDataProvider | None = None,
    secondary_provider: HourlyMarketDataProvider | None = None,
    now: datetime | None = None,
    validation_config: ValidationConfig | None = None,
    provider_config: ProviderConfig | None = None,
) -> MarketDataSelection:
    """Select a validated provider with controlled Kraken -> Binance failover."""

    checked = now or datetime.now(timezone.utc)
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    checked = checked.astimezone(timezone.utc)
    validation = validation_config or ValidationConfig.from_env()
    config = provider_config or ProviderConfig.from_env()
    primary_provider = primary_provider or KrakenProvider()
    secondary_provider = secondary_provider or BinanceProvider()

    primary = _attempt_provider(primary_provider, limit, now=checked, validation_config=validation)
    secondary = _attempt_provider(
        secondary_provider, limit, now=checked, validation_config=validation
    )

    comparison: dict[str, Any] | None = None
    if primary.data is not None and secondary.data is not None:
        comparison = compare_overlapping_closes(primary.data, secondary.data, config=config)
        if comparison["status"] == "disagreement":
            raise ProviderDisagreementError(
                "Market-data providers disagree: "
                f"max close difference {comparison['max_close_difference_pct']:.4f}% exceeds "
                f"{config.max_close_difference_pct:.4f}%"
            )

    if primary.healthy:
        return MarketDataSelection(
            data=primary.data,
            provider=primary.name,
            source="Kraken BTC/USD hourly OHLC",
            source_pair=primary.pair,
            fallback_used=False,
            comparison=comparison,
            primary=primary,
            secondary=secondary,
        )

    if secondary.healthy:
        # If primary returned data but failed quality checks, require enough
        # cross-provider overlap before trusting the fallback. A hard primary
        # outage has no data to compare, so a healthy secondary is allowed.
        if primary.data is not None:
            if comparison is None or comparison["status"] == "insufficient_overlap":
                raise ProviderDisagreementError(
                    "Primary data is unhealthy and there is insufficient overlap to validate failover"
                )
        return MarketDataSelection(
            data=secondary.data,
            provider=secondary.name,
            source="Binance BTCUSDT hourly klines (fallback)",
            source_pair=secondary.pair,
            fallback_used=True,
            comparison=comparison,
            primary=primary,
            secondary=secondary,
        )

    details = {
        "primary": primary.error or "unhealthy",
        "secondary": secondary.error or "unhealthy",
    }
    raise NoHealthyMarketDataProvider(f"No healthy market-data provider: {details}")


def persist_selection_report(
    selection: MarketDataSelection,
    path: Path = SOURCE_REPORT_PATH,
) -> None:
    path.write_text(json.dumps(selection.diagnostics(), indent=2, sort_keys=True) + "\n")


def fetch_redundant_hourly(limit: int = 512) -> MarketDataSelection:
    """Production entrypoint: select, validate, and persist provider provenance."""

    selection = select_market_data(limit)
    selected_attempt = selection.secondary if selection.fallback_used else selection.primary
    if selected_attempt.validation is not None:
        persist_validation_report(selected_attempt.validation)
    persist_selection_report(selection)
    return selection
