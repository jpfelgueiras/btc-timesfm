#!/usr/bin/env python3
"""Unit tests for redundant production market-data selection."""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import numpy as np

from unit_test_stubs import install_timesfm_stub

install_timesfm_stub()

from forecast_engine import MarketData  # noqa: E402
from market_data_sources import (  # noqa: E402
    NoHealthyMarketDataProvider,
    ProviderConfig,
    ProviderDisagreementError,
    fetch_bitstamp_hourly,
    select_market_data,
)
from market_data_validation import ValidationConfig  # noqa: E402


NOW = datetime(2026, 9, 5, 18, tzinfo=timezone.utc)


def make_market(
    *,
    count: int = 80,
    end_at: datetime = NOW,
    price_offset_pct: float = 0.0,
) -> MarketData:
    first = end_at - timedelta(hours=count - 1)
    timestamps = [int((first + timedelta(hours=i)).timestamp()) for i in range(count)]
    base = np.linspace(100_000.0, 101_000.0, count, dtype=np.float32)
    closes = base * (1.0 + price_offset_pct / 100.0)
    return MarketData(
        timestamps=timestamps,
        opens=closes - 20.0,
        highs=closes + 80.0,
        lows=closes - 80.0,
        closes=closes,
        volumes=np.linspace(100.0, 120.0, count, dtype=np.float32),
    )


@dataclass
class FakeProvider:
    name: str
    pair: str
    data: MarketData | None = None
    failure: Exception | None = None

    def fetch(self, limit: int) -> MarketData:
        del limit
        if self.failure is not None:
            raise self.failure
        assert self.data is not None
        return self.data


class MarketDataSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validation = ValidationConfig(
            min_candles=64,
            max_staleness_minutes=90.0,
            max_hourly_return_pct=20.0,
            max_candle_range_pct=30.0,
            max_volume_median_multiplier=50.0,
        )
        self.providers = ProviderConfig(
            max_close_difference_pct=0.75,
            comparison_candles=24,
            min_overlap_candles=6,
            volume_feature_cap_multiplier=10.0,
        )

    def select(self, primary: FakeProvider, secondary: FakeProvider):
        return select_market_data(
            64,
            primary_provider=primary,
            secondary_provider=secondary,
            now=NOW,
            validation_config=self.validation,
            provider_config=self.providers,
        )

    def test_healthy_primary_is_preferred(self) -> None:
        primary = FakeProvider("kraken", "BTC/USD", make_market())
        secondary = FakeProvider("bitstamp", "BTC/USD", make_market(price_offset_pct=0.05))

        selected = self.select(primary, secondary)

        self.assertEqual(selected.provider, "kraken")
        self.assertFalse(selected.fallback_used)
        self.assertEqual(selected.source_pair, "BTC/USD")
        self.assertEqual(selected.comparison["status"], "ok")

    def test_primary_outage_uses_healthy_fallback(self) -> None:
        primary = FakeProvider("kraken", "BTC/USD", failure=RuntimeError("503 unavailable"))
        secondary = FakeProvider("bitstamp", "BTC/USD", make_market())

        selected = self.select(primary, secondary)

        self.assertEqual(selected.provider, "bitstamp")
        self.assertTrue(selected.fallback_used)
        self.assertIn("fallback", selected.source)
        self.assertIsNone(selected.comparison)

    def test_stale_primary_uses_fallback_after_overlap_check(self) -> None:
        stale = make_market(end_at=NOW - timedelta(hours=3))
        healthy = make_market()
        primary = FakeProvider("kraken", "BTC/USD", stale)
        secondary = FakeProvider("bitstamp", "BTC/USD", healthy)

        selected = self.select(primary, secondary)

        self.assertEqual(selected.provider, "bitstamp")
        self.assertTrue(selected.fallback_used)
        self.assertFalse(selected.primary.healthy)
        self.assertEqual(selected.comparison["status"], "ok")
        self.assertGreaterEqual(selected.comparison["overlap_candles"], 6)

    def test_extreme_volume_primary_is_winsorized_and_kept(self) -> None:
        data = make_market()
        baseline = float(np.median(data.volumes[-24:-1]))
        data.volumes[-1] = baseline * 100.0
        primary = FakeProvider("kraken", "BTC/USD", data)
        secondary = FakeProvider("bitstamp", "BTC/USD", make_market())

        selected = self.select(primary, secondary)

        self.assertEqual(selected.provider, "kraken")
        self.assertTrue(selected.primary.healthy)
        self.assertLessEqual(float(selected.data.volumes[-1]), baseline * 10.01)
        metrics = selected.primary.validation.metrics
        self.assertGreaterEqual(metrics["volume_outliers_winsorized"], 1)
        self.assertEqual(metrics["volume_feature_cap_multiplier"], 10.0)
        self.assertEqual(metrics["soft_validation_warnings"][0]["code"], "extreme_volume")

    def test_provider_disagreement_fails_closed(self) -> None:
        primary = FakeProvider("kraken", "BTC/USD", make_market())
        secondary = FakeProvider("bitstamp", "BTC/USD", make_market(price_offset_pct=2.0))

        with self.assertRaises(ProviderDisagreementError):
            self.select(primary, secondary)

    def test_both_unhealthy_fail_closed(self) -> None:
        primary = FakeProvider("kraken", "BTC/USD", failure=RuntimeError("down"))
        secondary = FakeProvider(
            "bitstamp", "BTC/USD", make_market(end_at=NOW - timedelta(hours=4))
        )

        with self.assertRaises(NoHealthyMarketDataProvider):
            self.select(primary, secondary)

    def test_stale_primary_without_enough_overlap_is_rejected(self) -> None:
        primary = FakeProvider(
            "kraken",
            "BTC/USD",
            make_market(end_at=NOW - timedelta(hours=100)),
        )
        secondary = FakeProvider("bitstamp", "BTC/USD", make_market())

        with self.assertRaises(ProviderDisagreementError):
            self.select(primary, secondary)

    @patch("market_data_sources.requests.get")
    def test_bitstamp_hourly_parses_completed_candles(self, mock_get: Mock) -> None:
        first = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
        rows = []
        for index in range(80):
            close = 100_000.0 + index
            rows.append(
                {
                    "timestamp": str(first + index * 3600),
                    "open": str(close - 10.0),
                    "high": str(close + 20.0),
                    "low": str(close - 20.0),
                    "close": str(close),
                    "volume": str(100.0 + index),
                }
            )
        response = Mock()
        response.json.return_value = {"data": {"pair": "BTC/USD", "ohlc": rows}}
        response.raise_for_status.return_value = None
        mock_get.return_value = response

        data = fetch_bitstamp_hourly(64)

        self.assertEqual(len(data.timestamps), 80)
        self.assertEqual(data.timestamps[0], first + 3600)
        self.assertAlmostEqual(float(data.closes[-1]), 100_079.0, places=2)
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["step"], 3600)
        self.assertEqual(kwargs["params"]["exclude_current_candle"], "true")
        self.assertEqual(kwargs["params"]["limit"], 513)


if __name__ == "__main__":
    unittest.main()
