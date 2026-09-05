"""Redundant hourly BTC market-data providers for production forecasts.

Kraken remains the preferred BTC/USD source. Bitstamp BTC/USD is the secondary
source used only when the primary is unavailable or fails hard validation.
Large but otherwise valid volume spikes are treated as a soft production-data
warning: volume is winsorized before it reaches feature engineering, while
price/timestamp/OHLC validation remains fail-closed.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
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


BITSTAMP_OHLC_URL = "https://www.bitstamp.net/api/v2/ohlc/btcusd/"
SOURCE_REPORT_PATH = Path("market_data_source.json")


class HourlyMarketDataProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def pair(self) -> str: ...

    def fetch(self, limit: int) -> MarketData: ...


@dataclass(frozen=True)
class ProviderConfig:
    max_close_difference_pct: float = 0.75
    comparison_candles: int = 24
    min_overlap_candles: int = 6
    volume_feature_cap_multiplier: float = 10.0

    @classmethod
    def from_env(cls) -> "ProviderConfig":
        return cls(
            max_close_difference_pct=_positive_float(
                "BTC_PROVIDER_MAX_CLOSE_DIFF_PCT", cls.max_close_difference_pct
            ),
            comparison_candles=_positive_int(
                "BTC_PROVIDER_COMPARE_CANDLES", cls.comparison_candles
            ),
            min_overlap_candles=_positive_int("BTC_PROVIDER_MIN_OVERLAP", cls.min_overlap_candles),
            volume_feature_cap_multiplier=_positive_float(
                "BTC_PROVIDER_VOLUME_CAP_MULTIPLIER", cls.volume_feature_cap_multiplier
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
class BitstampProvider:
    name: str = "bitstamp"
    pair: str = "BTC/USD"

    def fetch(self, limit: int) -> MarketData:
        return fetch_bitstamp_hourly(limit)


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


def fetch_bitstamp_hourly(limit: int = 512) -> MarketData:
    """Fetch recent completed Bitstamp BTC/USD hourly candles.

    Bitstamp returns candle-open Unix timestamps. They are normalized to
    candle-close UTC seconds to match Kraken and the rest of the project.
    ``exclude_current_candle`` keeps the still-forming hour out of the input.
    """

    limit = max(limit, max(CONTEXT_WINDOWS) + 1)
    if limit > 1000:
        raise ValueError("Bitstamp OHLC endpoint supports at most 1000 candles")

    response = requests.get(
        BITSTAMP_OHLC_URL,
        params={
            "step": INTERVAL_MINUTES * 60,
            "limit": limit,
            "exclude_current_candle": "true",
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("data", {}).get("ohlc") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("Unexpected Bitstamp OHLC response")

    completed = sorted(
        (row for row in rows if isinstance(row, dict) and "timestamp" in row),
        key=lambda row: int(row["timestamp"]),
    )[-limit:]
    if len(completed) < 64:
        raise RuntimeError(f"Not enough completed Bitstamp candles: {len(completed)}")

    def arr(name: str) -> np.ndarray:
        return np.asarray([float(row[name]) for row in completed], dtype=np.float32)

    return MarketData(
        timestamps=[
            int(row["timestamp"]) + INTERVAL_MINUTES * 60 for row in completed
        ],
        opens=arr("open"),
        highs=arr("high"),
        lows=arr("low"),
        closes=arr("close"),
        volumes=arr("volume"),
    )


def _winsorize_extreme_volumes(
    data: MarketData,
    *,
    validation_config: ValidationConfig,
    cap_multiplier: float,
) -> int:
    """Cap volume-only outliers without altering prices or timestamps."""

    volumes = np.asarray(data.volumes, dtype=np.float64).copy()
    changed = 0
    for index in range(validation_config.volume_min_history, len(volumes)):
        start = max(0, index - validation_config.volume_lookback)
        baseline = float(np.median(volumes[start:index]))
        if not math.isfinite(baseline) or baseline <= 0:
            continue
        ceiling = baseline * cap_multiplier
        if volumes[index] > ceiling:
            volumes[index] = ceiling
            changed += 1
    data.volumes = volumes.astype(np.float32)
    return changed


def _attempt_provider(
    provider: HourlyMarketDataProvider,
    limit: int,
    *,
    now: datetime,
    validation_config: ValidationConfig,
    provider_config: ProviderConfig,
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
        error_codes = {error["code"] for error in exc.report.errors}
        if error_codes == {"extreme_volume"}:
            cap = min(
                provider_config.volume_feature_cap_multiplier,
                validation_config.max_volume_median_multiplier,
            )
            changed = _winsorize_extreme_volumes(
                data,
                validation_config=validation_config,
                cap_multiplier=cap,
            )
            try:
                report = validate_market_data(
                    data,
                    source=f"{provider.name} {provider.pair} hourly OHLCV",
                    now=now,
                    config=validation_config,
                    check_staleness=True,
                )
            except MarketDataValidationError as second_exc:
                return ProviderAttempt(
                    provider.name,
                    provider.pair,
                    data,
                    second_exc.report,
                    str(second_exc),
                )
            report.metrics["volume_outliers_winsorized"] = changed
            report.metrics["volume_feature_cap_multiplier"] = cap
            report.metrics["soft_validation_warnings"] = exc.report.errors
            return ProviderAttempt(provider.name, provider.pair, data, report, None)
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
    common = sorted(set(primary_closes).intersection(secondary_closes))[
        -config.comparison_candles :
    ]
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
    """Select a validated provider with controlled Kraken -> Bitstamp failover."""

    checked = now or datetime.now(timezone.utc)
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    checked = checked.astimezone(timezone.utc)
    validation = validation_config or ValidationConfig.from_env()
    config = provider_config or ProviderConfig.from_env()
    primary_impl: HourlyMarketDataProvider = (
        primary_provider if primary_provider is not None else KrakenProvider()
    )
    secondary_impl: HourlyMarketDataProvider = (
        secondary_provider if secondary_provider is not None else BitstampProvider()
    )

    primary = _attempt_provider(
        primary_impl,
        limit,
        now=checked,
        validation_config=validation,
        provider_config=config,
    )
    secondary = _attempt_provider(
        secondary_impl,
        limit,
        now=checked,
        validation_config=validation,
        provider_config=config,
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
        assert primary.data is not None
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
        assert secondary.data is not None
        return MarketDataSelection(
            data=secondary.data,
            provider=secondary.name,
            source="Bitstamp BTC/USD hourly OHLC (fallback)",
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
