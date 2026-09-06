#!/usr/bin/env python3
"""Unit tests for the research benchmark suite."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

from tests.support.unit_test_stubs import install_timesfm_stub

install_timesfm_stub()

from btc_timesfm.forecasting.benchmarks import BENCHMARK_NAMES, benchmark_forecasts, benchmark_metadata  # noqa: E402
from btc_timesfm.forecasting.forecast_engine import MarketData  # noqa: E402


def make_market(count: int = 200) -> MarketData:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    closes = np.asarray(
        [100.0 + i * 0.25 + (i % 24) * 0.05 for i in range(count)], dtype=np.float32
    )
    return MarketData(
        timestamps=[int((base + timedelta(hours=i)).timestamp()) for i in range(count)],
        opens=closes - 0.1,
        highs=closes + 0.4,
        lows=closes - 0.4,
        closes=closes,
        volumes=np.linspace(10.0, 20.0, count, dtype=np.float32),
    )


class BenchmarkTests(unittest.TestCase):
    def test_suite_contains_required_baseline_families(self) -> None:
        forecasts = benchmark_forecasts(make_market())
        self.assertEqual(tuple(forecasts), BENCHMARK_NAMES)
        self.assertIn("persistence", forecasts)
        self.assertIn("drift_7d", forecasts)
        self.assertIn("drift_24h", forecasts)
        self.assertIn("seasonal_naive_24h", forecasts)
        self.assertIn("ar1", forecasts)
        self.assertIn("ema_return_24h", forecasts)

    def test_every_benchmark_returns_all_horizons_and_positive_prices(self) -> None:
        forecasts = benchmark_forecasts(make_market())
        for model in forecasts.values():
            self.assertEqual(set(model), {"2h", "4h", "8h", "16h"})
            for output in model.values():
                self.assertGreater(output["price_usd"], 0.0)

    def test_persistence_always_equals_current_price(self) -> None:
        data = make_market()
        current = float(data.closes[-1])
        forecasts = benchmark_forecasts(data)
        for horizon in ("2h", "4h", "8h", "16h"):
            self.assertEqual(forecasts["persistence"][horizon]["price_usd"], current)

    def test_seasonal_naive_uses_only_previous_day_values(self) -> None:
        data = make_market(100)
        forecasts = benchmark_forecasts(data)
        current_index = len(data.closes) - 1
        for hour in (2, 4, 8, 16):
            expected = float(data.closes[current_index + hour - 24])
            self.assertEqual(forecasts["seasonal_naive_24h"][f"{hour}h"]["price_usd"], expected)

    def test_suite_is_deterministic_for_identical_input(self) -> None:
        data = make_market()
        self.assertEqual(benchmark_forecasts(data), benchmark_forecasts(data))

    def test_metadata_declares_persistence_as_primary_baseline(self) -> None:
        metadata = benchmark_metadata()
        self.assertEqual(metadata["primary_baseline"], "persistence")
        self.assertEqual(metadata["models"], list(BENCHMARK_NAMES))
        self.assertEqual(metadata["seasonal_period_hours"], 24)


if __name__ == "__main__":
    unittest.main()
