#!/usr/bin/env python3
"""Unit tests for forecast-engine feature, baseline, and calibration logic."""

from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np

from tests.support.unit_test_stubs import install_timesfm_stub

install_timesfm_stub()

from btc_timesfm.forecasting.forecast_engine import (  # noqa: E402
    CONTEXT_WINDOWS,
    MarketData,
    _forecast_prices_from_return_path,
    baseline_forecasts,
    detect_regime,
    empirical_calibration_multiplier,
    market_features,
    static_model_weights,
    timesfm_multi_context,
    _z_normalize,
    timesfm_multi_context_inventory,
)


def make_market(count: int = 513, *, start_price: float = 100.0) -> MarketData:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    closes = np.linspace(start_price, start_price + 5.0, count, dtype=np.float32)
    return MarketData(
        timestamps=[int((base + timedelta(hours=i)).timestamp()) for i in range(count)],
        opens=closes - 0.1,
        highs=closes + 0.5,
        lows=closes - 0.5,
        closes=closes,
        volumes=np.linspace(10.0, 20.0, count, dtype=np.float32),
    )


class FakeTimesFM:
    def __init__(self) -> None:
        self.context_lengths: list[int] = []

    def predict_batch(self, *, contexts, horizon, return_quantiles, use_symmetric_averaging):
        self.context_lengths = [len(context) for context in contexts]
        self.asserted_horizon = horizon
        self.asserted_quantiles = return_quantiles
        self.asserted_symmetric = use_symmetric_averaging
        results = []
        for _ in contexts:
            point = np.zeros(horizon, dtype=np.float64)
            quantiles = np.zeros((horizon, 9), dtype=np.float64)
            quantiles[:, 0] = -0.001
            quantiles[:, 4] = 0.0
            quantiles[:, 8] = 0.001
            results.append(SimpleNamespace(forecast=point, quantiles=quantiles))
        return results


class ForecastEngineTests(unittest.TestCase):
    def test_market_returns_are_log_differences(self) -> None:
        data = make_market(4)
        expected = np.diff(np.log(data.closes.astype(np.float64)))
        np.testing.assert_allclose(data.returns, expected.astype(np.float32), rtol=1e-6)

    def test_market_features_are_finite_and_include_time_signals(self) -> None:
        data = make_market()
        features = market_features(data)
        for key in (
            "volatility_6h_pct",
            "volatility_24h_pct",
            "volatility_7d_pct",
            "range_24h_avg_pct",
            "volume_zscore_7d",
            "rsi_14",
            "momentum_24h_pct",
        ):
            self.assertTrue(math.isfinite(float(features[key])), key)
        self.assertIn(features["hour_utc"], range(24))
        self.assertIn(features["weekday_utc"], range(7))

    def test_detect_regime_covers_all_three_states(self) -> None:
        self.assertEqual(
            detect_regime(
                {
                    "volatility_24h_pct": 2.0,
                    "volatility_7d_pct": 1.0,
                    "momentum_24h_pct": 0.1,
                    "rsi_14": 50,
                }
            ),
            "high_volatility",
        )
        self.assertEqual(
            detect_regime(
                {
                    "volatility_24h_pct": 0.5,
                    "volatility_7d_pct": 0.5,
                    "momentum_24h_pct": 3.5,
                    "rsi_14": 50,
                }
            ),
            "trending",
        )
        self.assertEqual(
            detect_regime(
                {
                    "volatility_24h_pct": 0.5,
                    "volatility_7d_pct": 0.5,
                    "momentum_24h_pct": 0.2,
                    "rsi_14": 50,
                }
            ),
            "range",
        )

    def test_return_path_is_reconstructed_into_horizon_prices(self) -> None:
        hourly = math.log(1.01)
        prices = _forecast_prices_from_return_path(100.0, np.full(16, hourly))
        self.assertAlmostEqual(prices["2h"], 100.0 * 1.01**2, places=8)
        self.assertAlmostEqual(prices["16h"], 100.0 * 1.01**16, places=8)

    def test_timesfm_uses_all_available_context_windows(self) -> None:
        data = make_market(513)
        model = FakeTimesFM()
        forecasts = timesfm_multi_context(model, data)
        self.assertEqual(model.context_lengths, list(CONTEXT_WINDOWS))
        self.assertEqual(set(forecasts), {"timesfm_168h", "timesfm_336h", "timesfm_512h"})
        current = float(data.closes[-1])
        for model_output in forecasts.values():
            self.assertAlmostEqual(model_output["2h"]["price_usd"], current, places=6)
            self.assertLess(model_output["2h"]["q10_usd"], current)
            self.assertGreater(model_output["2h"]["q90_usd"], current)

    def test_z_normalize_function(self) -> None:
        # Test with normal data
        arr = np.array([1.0, 2.0, 3.0, 4.0, 5.0], dtype=np.float32)
        normalized, mean, std = _z_normalize(arr)
        self.assertAlmostEqual(mean, 3.0, places=6)
        self.assertAlmostEqual(std, 1.5811388300841898, places=6)  # std with ddof=1
        expected = np.array([-1.26491106, -0.63245553, 0.0, 0.63245553, 1.26491106], dtype=np.float32)
        np.testing.assert_allclose(normalized, expected, rtol=1e-6)

        # Test with constant data (std should be 0)
        const_arr = np.array([5.0, 5.0, 5.0, 5.0], dtype=np.float32)
        normalized_const, mean_const, std_const = _z_normalize(const_arr)
        self.assertAlmostEqual(mean_const, 5.0, places=6)
        self.assertAlmostEqual(std_const, 0.0, places=6)
        np.testing.assert_array_equal(normalized_const, np.array([0.0, 0.0, 0.0, 0.0], dtype=np.float32))

        # Test with empty array
        empty_arr = np.array([], dtype=np.float32)
        normalized_empty, mean_empty, std_empty = _z_normalize(empty_arr)
        self.assertEqual(len(normalized_empty), 0)
        self.assertEqual(mean_empty, 0.0)
        self.assertEqual(std_empty, 0.0)

    def test_timesfm_multi_context_inventory_basic(self) -> None:
        data = make_market(513)
        model = FakeTimesFM()
        forecasts, metadata = timesfm_multi_context_inventory(
            model, data, context_lengths=(64, 168, 336, 512, 1024), normalize=False, symmetric_averaging=False
        )
        # Should have forecasts for available contexts
        self.assertEqual(set(forecasts.keys()), {"timesfm_64h", "timesfm_168h", "timesfm_336h", "timesfm_512h"})
        # Should have metadata for each forecast
        self.assertEqual(set(metadata.keys()), {"timesfm_64h", "timesfm_168h", "timesfm_336h", "timesfm_512h"})
        current = float(data.closes[-1])
        for model_output in forecasts.values():
            self.assertAlmostEqual(model_output["2h"]["price_usd"], current, places=6)
            self.assertLess(model_output["2h"]["q10_usd"], current)
            self.assertGreater(model_output["2h"]["q90_usd"], current)
        # Check metadata contents
        for window in [64, 168, 336, 512]:
            name = f"timesfm_{window}h"
            self.assertEqual(metadata[name]["context_length_returns"], window)
            self.assertEqual(metadata[name]["context_length_hours"], window + 1)
            self.assertFalse(metadata[name]["normalization_applied"])
            self.assertFalse(metadata[name]["symmetric_averaging_applied"])
            self.assertIsNone(metadata[name]["context_mean"])
            self.assertIsNone(metadata[name]["context_std"])
            self.assertEqual(metadata[name]["actual_returns_available"], window)

    def test_timesfm_multi_context_inventory_with_normalization(self) -> None:
        data = make_market(513)
        model = FakeTimesFM()
        forecasts, metadata = timesfm_multi_context_inventory(
            model, data, context_lengths=(64, 168, 336, 512, 1024), normalize=True, symmetric_averaging=False, inner_context_count=2
        )
        # Should have forecasts for available contexts
        self.assertEqual(set(forecasts.keys()), {"timesfm_64h", "timesfm_168h", "timesfm_336h", "timesfm_512h"})
        # With inner_context_count=2 and sorted [64,168,336,512], inner should be [168,336]
        current = float(data.closes[-1])
        for model_output in forecasts.values():
            self.assertAlmostEqual(model_output["2h"]["price_usd"], current, places=6)
        # Check that normalization was applied to inner contexts (168, 336) but not outer (64, 512)
        self.assertFalse(metadata["timesfm_64h"]["normalization_applied"])
        self.assertTrue(metadata["timesfm_168h"]["normalization_applied"])
        self.assertTrue(metadata["timesfm_336h"]["normalization_applied"])
        self.assertFalse(metadata["timesfm_512h"]["normalization_applied"])
        # Mean and std should be set for normalized contexts
        self.assertIsNotNone(metadata["timesfm_168h"]["context_mean"])
        self.assertIsNotNone(metadata["timesfm_168h"]["context_std"])
        self.assertIsNotNone(metadata["timesfm_336h"]["context_mean"])
        self.assertIsNotNone(metadata["timesfm_336h"]["context_std"])
        # Mean and std should be None for non-normalized contexts
        self.assertIsNone(metadata["timesfm_64h"]["context_mean"])
        self.assertIsNone(metadata["timesfm_64h"]["context_std"])
        self.assertIsNone(metadata["timesfm_512h"]["context_mean"])
        self.assertIsNone(metadata["timesfm_512h"]["context_std"])

    def test_timesfm_multi_context_inventory_with_symmetric_averaging(self) -> None:
        data = make_market(513)
        model = FakeTimesFM()
        forecasts, metadata = timesfm_multi_context_inventory(
            model, data, context_lengths=(64, 168, 336, 512, 1024), normalize=False, symmetric_averaging=True, inner_context_count=2
        )
        # With inner_context_count=2 and sorted [64,168,336,512], inner should be [168,336]
        # Check that symmetric averaging was applied to inner contexts (168, 336) but not outer (64, 512)
        self.assertFalse(metadata["timesfm_64h"]["symmetric_averaging_applied"])
        self.assertTrue(metadata["timesfm_168h"]["symmetric_averaging_applied"])
        self.assertTrue(metadata["timesfm_336h"]["symmetric_averaging_applied"])
        self.assertFalse(metadata["timesfm_512h"]["symmetric_averaging_applied"])

    def test_timesfm_multi_context_inventory_fallback_to_all_returns(self) -> None:
        # Test when we don't have enough data for any of the requested context lengths
        data = make_market(50)  # 50 closes = 49 returns
        model = FakeTimesFM()
        forecasts, metadata = timesfm_multi_context_inventory(
            model, data, context_lengths=(64, 168, 336, 512, 1024), normalize=False, symmetric_averaging=False
        )
        # Should fall back to using all available returns (49)
        self.assertEqual(set(forecasts.keys()), {"timesfm_49h"})
        self.assertEqual(set(metadata.keys()), {"timesfm_49h"})
        self.assertEqual(metadata["timesfm_49h"]["context_length_returns"], 49)
        self.assertEqual(metadata["timesfm_49h"]["context_length_hours"], 50)
        # Since we only have one context, it's not considered "inner" for normalization/symmetric averaging
        self.assertFalse(metadata["timesfm_49h"]["normalization_applied"])
        self.assertFalse(metadata["timesfm_49h"]["symmetric_averaging_applied"])

    def test_baselines_include_persistence_drift_and_ar1(self) -> None:
        data = make_market()
        forecasts = baseline_forecasts(data)
        self.assertEqual(set(forecasts), {"persistence", "drift_7d", "ar1"})
        current = float(data.closes[-1])
        for horizon in ("2h", "4h", "8h", "16h"):
            self.assertEqual(forecasts["persistence"][horizon]["price_usd"], current)
            for model_output in forecasts.values():
                self.assertGreater(model_output[horizon]["price_usd"], 0.0)

    def test_static_weights_are_normalized_for_each_regime(self) -> None:
        names = ["timesfm_168h", "timesfm_336h", "timesfm_512h", "persistence", "drift_7d", "ar1"]
        for regime in ("range", "trending", "high_volatility"):
            weights = static_model_weights(names, regime)
            self.assertAlmostEqual(sum(weights.values()), 1.0, places=10)
            self.assertEqual(set(weights), set(names))
        self.assertGreater(
            static_model_weights(names, "range")["persistence"],
            static_model_weights(names, "trending")["persistence"],
        )

    def test_calibration_waits_for_ten_matured_samples(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        history = []
        actuals = {}
        for i in range(9):
            origin = start + timedelta(hours=i)
            history.append(
                {
                    "latest_close_at": origin.isoformat(),
                    "predictions": {"2h": {"q10_usd": 90.0, "q90_usd": 110.0}},
                }
            )
            actuals[int((origin + timedelta(hours=2)).timestamp())] = 100.0
        multiplier, samples, coverage = empirical_calibration_multiplier(history, actuals, 2)
        self.assertEqual(multiplier, 1.0)
        self.assertEqual(samples, 9)
        self.assertIsNone(coverage)

    def test_calibration_adjusts_from_empirical_coverage(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        history = []
        actuals = {}
        for i in range(10):
            origin = start + timedelta(hours=i)
            history.append(
                {
                    "latest_close_at": origin.isoformat(),
                    "predictions": {"2h": {"q10_usd": 90.0, "q90_usd": 110.0}},
                }
            )
            actuals[int((origin + timedelta(hours=2)).timestamp())] = 100.0
        multiplier, samples, coverage = empirical_calibration_multiplier(history, actuals, 2)
        self.assertEqual(samples, 10)
        self.assertEqual(coverage, 1.0)
        self.assertAlmostEqual(multiplier, math.sqrt(0.8), places=8)


if __name__ == "__main__":
    unittest.main()
