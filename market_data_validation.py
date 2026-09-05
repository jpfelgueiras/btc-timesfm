"""Strict market-data validation for production forecasts and research runs.

The validator is intentionally independent from TimesFM so it can be exercised
with lightweight unit tests and reused by Kraken production data, Binance
backtests, and the weekly optimizer.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, Sequence

import numpy as np


VALIDATION_REPORT_PATH = Path("market_data_validation.json")


class MarketDataLike(Protocol):
    timestamps: Sequence[int]
    opens: Any
    highs: Any
    lows: Any
    closes: Any
    volumes: Any


@dataclass(frozen=True)
class ValidationConfig:
    interval_seconds: int = 3600
    min_candles: int = 64
    max_staleness_minutes: float = 90.0
    future_tolerance_seconds: int = 60
    max_hourly_return_pct: float = 20.0
    max_candle_range_pct: float = 30.0
    max_volume_median_multiplier: float = 50.0
    volume_lookback: int = 168
    volume_min_history: int = 24

    @classmethod
    def from_env(cls) -> "ValidationConfig":
        """Load conservative thresholds from environment overrides."""
        return cls(
            interval_seconds=_env_int("BTC_DATA_INTERVAL_SECONDS", cls.interval_seconds),
            min_candles=_env_int("BTC_DATA_MIN_CANDLES", cls.min_candles),
            max_staleness_minutes=_env_float(
                "BTC_DATA_MAX_STALENESS_MINUTES", cls.max_staleness_minutes
            ),
            future_tolerance_seconds=_env_int(
                "BTC_DATA_FUTURE_TOLERANCE_SECONDS", cls.future_tolerance_seconds
            ),
            max_hourly_return_pct=_env_float(
                "BTC_DATA_MAX_HOURLY_RETURN_PCT", cls.max_hourly_return_pct
            ),
            max_candle_range_pct=_env_float(
                "BTC_DATA_MAX_CANDLE_RANGE_PCT", cls.max_candle_range_pct
            ),
            max_volume_median_multiplier=_env_float(
                "BTC_DATA_MAX_VOLUME_MEDIAN_MULTIPLIER",
                cls.max_volume_median_multiplier,
            ),
            volume_lookback=_env_int("BTC_DATA_VOLUME_LOOKBACK", cls.volume_lookback),
            volume_min_history=_env_int(
                "BTC_DATA_VOLUME_MIN_HISTORY", cls.volume_min_history
            ),
        )


@dataclass
class ValidationReport:
    source: str
    status: str
    checked_at: str
    candle_count: int
    first_timestamp: int | None
    latest_timestamp: int | None
    metrics: dict[str, Any]
    errors: list[dict[str, Any]]
    config: dict[str, Any]

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MarketDataValidationError(RuntimeError):
    def __init__(self, report: ValidationReport):
        self.report = report
        codes = ", ".join(error["code"] for error in report.errors)
        super().__init__(
            f"Market data validation failed for {report.source}: {codes or 'unknown error'}"
        )


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return float(default)
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return parsed


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return int(default)
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _iso_timestamp(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _sample_indices(indices: np.ndarray, limit: int = 5) -> list[int]:
    return [int(value) for value in indices[:limit]]


def _add_error(
    errors: list[dict[str, Any]],
    code: str,
    message: str,
    *,
    count: int = 1,
    samples: list[Any] | None = None,
) -> None:
    item: dict[str, Any] = {
        "code": code,
        "message": message,
        "count": int(count),
    }
    if samples:
        item["samples"] = samples
    errors.append(item)


def validate_market_data(
    data: MarketDataLike,
    *,
    source: str,
    now: datetime | None = None,
    config: ValidationConfig | None = None,
    check_staleness: bool = True,
) -> ValidationReport:
    """Validate OHLCV structure, cadence, freshness, and hard anomalies.

    Any error makes the report fail and raises ``MarketDataValidationError``.
    Thresholds are intentionally conservative: they are meant to catch corrupt
    or clearly implausible inputs, not classify ordinary market volatility.
    """
    cfg = config or ValidationConfig.from_env()
    checked = now or datetime.now(timezone.utc)
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    checked = checked.astimezone(timezone.utc)

    errors: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {}

    timestamps = np.asarray(list(data.timestamps), dtype=np.int64)
    arrays = {
        "open": np.asarray(data.opens, dtype=np.float64),
        "high": np.asarray(data.highs, dtype=np.float64),
        "low": np.asarray(data.lows, dtype=np.float64),
        "close": np.asarray(data.closes, dtype=np.float64),
        "volume": np.asarray(data.volumes, dtype=np.float64),
    }

    lengths = {
        "timestamps": len(timestamps),
        **{name: len(values) for name, values in arrays.items()},
    }
    unique_lengths = set(lengths.values())
    if len(unique_lengths) != 1:
        _add_error(
            errors,
            "length_mismatch",
            f"OHLCV arrays have inconsistent lengths: {lengths}",
        )

    count = min(lengths.values()) if lengths else 0
    first_timestamp = int(timestamps[0]) if len(timestamps) else None
    latest_timestamp = int(timestamps[-1]) if len(timestamps) else None

    if count < cfg.min_candles:
        _add_error(
            errors,
            "insufficient_candles",
            f"Need at least {cfg.min_candles} candles, got {count}",
        )

    if len(timestamps):
        nonpositive_ts = np.where(timestamps <= 0)[0]
        if len(nonpositive_ts):
            _add_error(
                errors,
                "invalid_timestamp",
                "Timestamps must be positive Unix seconds",
                count=len(nonpositive_ts),
                samples=_sample_indices(nonpositive_ts),
            )

    for name, values in arrays.items():
        bad = np.where(~np.isfinite(values))[0]
        if len(bad):
            _add_error(
                errors,
                f"non_finite_{name}",
                f"{name} contains NaN or infinity",
                count=len(bad),
                samples=_sample_indices(bad),
            )

    if len(unique_lengths) == 1 and count:
        opens = arrays["open"]
        highs = arrays["high"]
        lows = arrays["low"]
        closes = arrays["close"]
        volumes = arrays["volume"]

        for name, values in (
            ("open", opens),
            ("high", highs),
            ("low", lows),
            ("close", closes),
        ):
            bad = np.where(values <= 0)[0]
            if len(bad):
                _add_error(
                    errors,
                    f"non_positive_{name}",
                    f"{name} prices must be positive",
                    count=len(bad),
                    samples=_sample_indices(bad),
                )

        bad_volume = np.where(volumes <= 0)[0]
        if len(bad_volume):
            _add_error(
                errors,
                "non_positive_volume",
                "Volume must be positive",
                count=len(bad_volume),
                samples=_sample_indices(bad_volume),
            )

        if (
            np.all(np.isfinite(opens))
            and np.all(np.isfinite(highs))
            and np.all(np.isfinite(lows))
            and np.all(np.isfinite(closes))
        ):
            invalid_ohlc = np.where(
                (highs < opens)
                | (highs < closes)
                | (highs < lows)
                | (lows > opens)
                | (lows > closes)
                | (lows > highs)
            )[0]
            if len(invalid_ohlc):
                _add_error(
                    errors,
                    "invalid_ohlc",
                    "OHLC relationship is impossible (high/low do not bound open/close)",
                    count=len(invalid_ohlc),
                    samples=_sample_indices(invalid_ohlc),
                )

            if np.all(closes > 0) and len(closes) > 1:
                hourly_returns = np.abs(closes[1:] / closes[:-1] - 1.0) * 100.0
                max_return = float(np.max(hourly_returns))
                metrics["max_abs_hourly_return_pct"] = round(max_return, 6)
                extreme = np.where(hourly_returns > cfg.max_hourly_return_pct)[0] + 1
                if len(extreme):
                    _add_error(
                        errors,
                        "extreme_hourly_return",
                        "Absolute close-to-close return exceeds "
                        f"{cfg.max_hourly_return_pct:.2f}%",
                        count=len(extreme),
                        samples=_sample_indices(extreme),
                    )

            if np.all(lows > 0):
                candle_ranges = (highs / lows - 1.0) * 100.0
                max_range = float(np.max(candle_ranges))
                metrics["max_candle_range_pct"] = round(max_range, 6)
                extreme = np.where(candle_ranges > cfg.max_candle_range_pct)[0]
                if len(extreme):
                    _add_error(
                        errors,
                        "extreme_candle_range",
                        f"High-low candle range exceeds {cfg.max_candle_range_pct:.2f}%",
                        count=len(extreme),
                        samples=_sample_indices(extreme),
                    )

        if np.all(np.isfinite(volumes)) and np.all(volumes > 0):
            max_ratio = 1.0
            volume_outliers: list[int] = []
            for index in range(cfg.volume_min_history, len(volumes)):
                start = max(0, index - cfg.volume_lookback)
                baseline = float(np.median(volumes[start:index]))
                if baseline <= 0:
                    continue
                ratio = float(volumes[index] / baseline)
                max_ratio = max(max_ratio, ratio)
                if ratio > cfg.max_volume_median_multiplier:
                    volume_outliers.append(index)
            metrics["max_volume_median_multiplier"] = round(max_ratio, 6)
            if volume_outliers:
                _add_error(
                    errors,
                    "extreme_volume",
                    "Volume exceeds rolling-median anomaly threshold "
                    f"({cfg.max_volume_median_multiplier:.2f}x)",
                    count=len(volume_outliers),
                    samples=volume_outliers[:5],
                )

    if len(timestamps) > 1:
        diffs = np.diff(timestamps)
        duplicate = np.where(diffs == 0)[0] + 1
        out_of_order = np.where(diffs < 0)[0] + 1
        irregular = np.where(
            (diffs > 0) & (diffs != cfg.interval_seconds)
        )[0] + 1

        if len(duplicate):
            _add_error(
                errors,
                "duplicate_timestamp",
                "Duplicate candle timestamps detected",
                count=len(duplicate),
                samples=_sample_indices(duplicate),
            )
        if len(out_of_order):
            _add_error(
                errors,
                "out_of_order_timestamp",
                "Candle timestamps are not strictly increasing",
                count=len(out_of_order),
                samples=_sample_indices(out_of_order),
            )
        if len(irregular):
            gaps = [
                {
                    "index": int(index),
                    "previous": _iso_timestamp(int(timestamps[index - 1])),
                    "current": _iso_timestamp(int(timestamps[index])),
                    "gap_seconds": int(timestamps[index] - timestamps[index - 1]),
                }
                for index in irregular[:5]
            ]
            _add_error(
                errors,
                "missing_or_irregular_candle",
                f"Expected exactly {cfg.interval_seconds} seconds between candles",
                count=len(irregular),
                samples=gaps,
            )

    if latest_timestamp is not None:
        checked_epoch = checked.timestamp()
        age_seconds = checked_epoch - latest_timestamp
        metrics["latest_age_seconds"] = round(float(age_seconds), 3)
        if latest_timestamp > checked_epoch + cfg.future_tolerance_seconds:
            _add_error(
                errors,
                "future_timestamp",
                "Latest candle timestamp is unexpectedly in the future",
            )
        if check_staleness and age_seconds > cfg.max_staleness_minutes * 60.0:
            _add_error(
                errors,
                "stale_data",
                "Latest completed candle is stale: "
                f"{age_seconds / 60.0:.1f} minutes old "
                f"(limit {cfg.max_staleness_minutes:.1f})",
            )

    report = ValidationReport(
        source=source,
        status="failed" if errors else "ok",
        checked_at=checked.isoformat(),
        candle_count=count,
        first_timestamp=first_timestamp,
        latest_timestamp=latest_timestamp,
        metrics=metrics,
        errors=errors,
        config=asdict(cfg),
    )
    if errors:
        raise MarketDataValidationError(report)
    return report


def persist_validation_report(
    report: ValidationReport,
    path: Path = VALIDATION_REPORT_PATH,
) -> None:
    path.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")


def print_validation_report(report: ValidationReport) -> None:
    print("Market data validation:")
    print(json.dumps(report.to_dict(), indent=2))
