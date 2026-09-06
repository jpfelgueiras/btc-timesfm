#!/usr/bin/env python3
"""Unit tests for strict OHLCV validation."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from btc_timesfm.data.market_data_validation import (
    MarketDataValidationError,
    ValidationConfig,
    trim_incomplete_trailing_candles,
    validate_market_data,
)


def make_market(count: int = 100):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    timestamps = [int((base + timedelta(hours=i)).timestamp()) for i in range(count)]
    closes = np.linspace(100.0, 110.0, count, dtype=np.float64)
    return SimpleNamespace(
        timestamps=timestamps,
        opens=closes - 0.1,
        highs=closes + 0.5,
        lows=closes - 0.5,
        closes=closes,
        volumes=np.linspace(10.0, 20.0, count, dtype=np.float64),
    )


def current_time_for(data, minutes_after_latest: int = 30) -> datetime:
    return datetime.fromtimestamp(data.timestamps[-1], tz=timezone.utc) + timedelta(
        minutes=minutes_after_latest
    )


class MarketDataValidationTests(unittest.TestCase):
    def test_valid_hourly_market_data_passes(self) -> None:
        data = make_market()
        report = validate_market_data(data, source="unit-test", now=current_time_for(data))
        self.assertTrue(report.ok)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.candle_count, 100)

    def test_duplicate_timestamp_is_rejected(self) -> None:
        data = make_market()
        data.timestamps[50] = data.timestamps[49]
        with self.assertRaises(MarketDataValidationError) as ctx:
            validate_market_data(data, source="unit-test", now=current_time_for(data))
        self.assertIn("duplicate_timestamp", {item["code"] for item in ctx.exception.report.errors})

    def test_missing_hour_is_rejected(self) -> None:
        data = make_market()
        for index in range(50, len(data.timestamps)):
            data.timestamps[index] += 3600
        with self.assertRaises(MarketDataValidationError) as ctx:
            validate_market_data(data, source="unit-test", now=current_time_for(data))
        self.assertIn(
            "missing_or_irregular_candle", {item["code"] for item in ctx.exception.report.errors}
        )

    def test_out_of_order_timestamp_is_rejected(self) -> None:
        data = make_market()
        data.timestamps[50], data.timestamps[51] = data.timestamps[51], data.timestamps[50]
        with self.assertRaises(MarketDataValidationError) as ctx:
            validate_market_data(data, source="unit-test", now=current_time_for(data))
        self.assertIn(
            "out_of_order_timestamp", {item["code"] for item in ctx.exception.report.errors}
        )

    def test_stale_live_data_is_rejected(self) -> None:
        data = make_market()
        with self.assertRaises(MarketDataValidationError) as ctx:
            validate_market_data(
                data,
                source="unit-test",
                now=current_time_for(data, minutes_after_latest=180),
            )
        self.assertIn("stale_data", {item["code"] for item in ctx.exception.report.errors})

    def test_historical_data_can_skip_staleness_check(self) -> None:
        data = make_market()
        report = validate_market_data(
            data,
            source="unit-test",
            now=current_time_for(data, minutes_after_latest=10_000),
            check_staleness=False,
        )
        self.assertTrue(report.ok)

    def test_incomplete_trailing_candle_is_trimmed_for_research(self) -> None:
        data = make_market()
        now = current_time_for(data)
        future_close = int((now + timedelta(minutes=30)).timestamp())
        data.timestamps.append(future_close)
        data.opens = np.append(data.opens, data.closes[-1])
        data.highs = np.append(data.highs, data.closes[-1] + 0.2)
        data.lows = np.append(data.lows, data.closes[-1] - 0.2)
        data.closes = np.append(data.closes, data.closes[-1] + 0.1)
        data.volumes = np.append(data.volumes, data.volumes[-1])

        removed = trim_incomplete_trailing_candles(data, now=now)

        self.assertEqual(removed, 1)
        self.assertLessEqual(data.timestamps[-1], int(now.timestamp()))
        report = validate_market_data(
            data,
            source="unit-test",
            now=now,
            check_staleness=False,
        )
        self.assertTrue(report.ok)

    def test_impossible_ohlc_is_rejected(self) -> None:
        data = make_market()
        data.highs[25] = data.lows[25] - 1.0
        with self.assertRaises(MarketDataValidationError) as ctx:
            validate_market_data(data, source="unit-test", now=current_time_for(data))
        self.assertIn("invalid_ohlc", {item["code"] for item in ctx.exception.report.errors})

    def test_non_positive_price_and_volume_are_rejected(self) -> None:
        data = make_market()
        data.closes[20] = 0.0
        data.volumes[30] = 0.0
        with self.assertRaises(MarketDataValidationError) as ctx:
            validate_market_data(data, source="unit-test", now=current_time_for(data))
        codes = {item["code"] for item in ctx.exception.report.errors}
        self.assertIn("non_positive_close", codes)
        self.assertIn("non_positive_volume", codes)

    def test_extreme_price_jump_is_rejected(self) -> None:
        data = make_market()
        data.opens[70] = data.closes[69]
        data.closes[70] = data.closes[69] * 1.30
        data.highs[70] = data.closes[70] * 1.001
        data.lows[70] = data.opens[70] * 0.999
        with self.assertRaises(MarketDataValidationError) as ctx:
            validate_market_data(data, source="unit-test", now=current_time_for(data))
        self.assertIn(
            "extreme_hourly_return", {item["code"] for item in ctx.exception.report.errors}
        )

    def test_extreme_volume_is_rejected(self) -> None:
        data = make_market()
        data.volumes[80] = np.median(data.volumes[20:80]) * 100.0
        with self.assertRaises(MarketDataValidationError) as ctx:
            validate_market_data(data, source="unit-test", now=current_time_for(data))
        self.assertIn("extreme_volume", {item["code"] for item in ctx.exception.report.errors})

    def test_thresholds_can_be_overridden_from_environment(self) -> None:
        with patch.dict(
            os.environ,
            {
                "BTC_DATA_MAX_HOURLY_RETURN_PCT": "12.5",
                "BTC_DATA_MAX_STALENESS_MINUTES": "120",
            },
            clear=False,
        ):
            config = ValidationConfig.from_env()
        self.assertEqual(config.max_hourly_return_pct, 12.5)
        self.assertEqual(config.max_staleness_minutes, 120.0)


if __name__ == "__main__":
    unittest.main()
