#!/usr/bin/env python3
"""Unit tests for forecast-engine feature, baseline, and calibration logic."""

from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np

from unit_test_stubs import install_timesfm_stub

install_timesfm_stub()

from forecast_engine import (  # noqa: E402
    CONTEXT_WINDOWS,
    MarketData,
    _forecast_prices_from_return_path,
    baseline_forecasts,
    detect_regime,
    empirical_calibration_multiplier,
    market_features,
    static_model_weights,
    timesfm_multi_context,
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
