#!/usr/bin/env python3
"""Unit tests for redundant production market-data selection."""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from unit_test_stubs import install_timesfm_stub

install_timesfm_stub()

from forecast_engine import MarketData  # noqa: E402
from market_data_sources import (  # noqa: E402
    NoHealthyMarketDataProvider,
    ProviderConfig,
    ProviderDisagreementError,
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
        secondary = FakeProvider("binance", "BTC/USDT", make_market(price_offset_pct=0.05))

        selected = self.select(primary, secondary)

        self.assertEqual(selected.provider, "kraken")
        self.assertFalse(selected.fallback_used)
        self.assertEqual(selected.source_pair, "BTC/USD")
        self.assertEqual(selected.comparison["status"], "ok")

    def test_primary_outage_uses_healthy_fallback(self) -> None:
        primary = FakeProvider("kraken", "BTC/USD", failure=RuntimeError("503 unavailable"))
        secondary = FakeProvider("binance", "BTC/USDT", make_market())

        selected = self.select(primary, secondary)

        self.assertEqual(selected.provider, "binance")
        self.assertTrue(selected.fallback_used)
        self.assertIn("fallback", selected.source)
        self.assertIsNone(selected.comparison)

    def test_stale_primary_uses_fallback_after_overlap_check(self) -> None:
        stale = make_market(end_at=NOW - timedelta(hours=3))
        healthy = make_market()
        primary = FakeProvider("kraken", "BTC/USD", stale)
        secondary = FakeProvider("binance", "BTC/USDT", healthy)

        selected = self.select(primary, secondary)

        self.assertEqual(selected.provider, "binance")
        self.assertTrue(selected.fallback_used)
        self.assertFalse(selected.primary.healthy)
        self.assertEqual(selected.comparison["status"], "ok")
        self.assertGreaterEqual(selected.comparison["overlap_candles"], 6)

    def test_provider_disagreement_fails_closed(self) -> None:
        primary = FakeProvider("kraken", "BTC/USD", make_market())
        secondary = FakeProvider("binance", "BTC/USDT", make_market(price_offset_pct=2.0))

        with self.assertRaises(ProviderDisagreementError):
            self.select(primary, secondary)

    def test_both_unhealthy_fail_closed(self) -> None:
        primary = FakeProvider("kraken", "BTC/USD", failure=RuntimeError("down"))
        secondary = FakeProvider(
            "binance", "BTC/USDT", make_market(end_at=NOW - timedelta(hours=4))
        )

        with self.assertRaises(NoHealthyMarketDataProvider):
            self.select(primary, secondary)

    def test_stale_primary_without_enough_overlap_is_rejected(self) -> None:
        primary = FakeProvider(
            "kraken",
            "BTC/USD",
            make_market(end_at=NOW - timedelta(hours=100)),
        )
        secondary = FakeProvider("binance", "BTC/USDT", make_market())

        with self.assertRaises(ProviderDisagreementError):
            self.select(primary, secondary)


if __name__ == "__main__":
    unittest.main()
