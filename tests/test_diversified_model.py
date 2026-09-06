#!/usr/bin/env python3
"""Tests for the lightweight diversified forecasting model."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from diversified_model import (
    DEFAULT_INITIAL_PRIOR_WEIGHT,
    MAX_INITIAL_PRIOR_WEIGHT,
    MAX_LAG,
    MODEL_NAME,
    TARGET_HOURS,
    augment_baselines,
    diversified_static_model_weights,
    production_enabled,
    ridge_feature_forecast,
    training_examples,
)


def make_market(count: int = 513) -> SimpleNamespace:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    index = np.arange(count, dtype=float)
    closes = 100.0 * np.exp(0.0003 * index + 0.01 * np.sin(index / 12.0))
    opens = closes * (1.0 - 0.0005)
    highs = closes * (1.0 + 0.002)
    lows = closes * (1.0 - 0.002)
    volumes = 1000.0 + 100.0 * np.sin(index / 7.0) + index * 0.2
    return SimpleNamespace(
        timestamps=[int((start + timedelta(hours=int(i))).timestamp()) for i in index],
        opens=opens,
        highs=highs,
        lows=lows,
        closes=closes,
        volumes=volumes,
    )


class DiversifiedModelTests(unittest.TestCase):
    def test_training_labels_never_extend_beyond_available_market_window(self) -> None:
        data = make_market()
        for horizon in TARGET_HOURS:
            _, _, indices = training_examples(data, horizon)
            self.assertGreaterEqual(indices[0], MAX_LAG)
            self.assertLessEqual(indices[-1] + horizon, len(data.closes) - 1)

    def test_forecast_covers_all_horizons_with_positive_prices(self) -> None:
        forecast = ridge_feature_forecast(make_market())
        self.assertEqual(set(forecast), {"2h", "4h", "8h", "16h"})
        for value in forecast.values():
            self.assertGreater(value["price_usd"], 0.0)
            self.assertGreater(value["training_samples"], 90)

    def test_same_market_window_is_deterministic(self) -> None:
        data = make_market()
        self.assertEqual(ridge_feature_forecast(data), ridge_feature_forecast(data))

    def test_future_extension_does_not_change_examples_available_at_prior_origin(self) -> None:
        original = make_market(300)
        extended = make_market(320)
        horizon = 16
        x_original, y_original, indices_original = training_examples(original, horizon)
        cutoff = len(original.closes) - 1 - horizon
        x_extended, y_extended, indices_extended = training_examples(extended, horizon)
        retained = [i for i, index in enumerate(indices_extended) if index <= cutoff]
        self.assertEqual(indices_original, [indices_extended[i] for i in retained])
        np.testing.assert_allclose(x_original, x_extended[retained], rtol=0.0, atol=1e-12)
        np.testing.assert_allclose(y_original, y_extended[retained], rtol=0.0, atol=1e-12)

    def test_short_history_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ridge_feature_forecast(make_market(100))

    def test_production_model_is_opt_in(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BTC_ENABLE_DIVERSIFIED_MODEL", None)
            self.assertFalse(production_enabled())
        with patch.dict(os.environ, {"BTC_ENABLE_DIVERSIFIED_MODEL": "true"}):
            self.assertTrue(production_enabled())

    def test_diversified_prior_reserves_weight_and_preserves_base_ratios(self) -> None:
        def base(model_names: list[str], _regime: str) -> dict[str, float]:
            raw = {"model_a": 0.6, "model_b": 0.4}
            return {name: raw[name] for name in model_names}

        weights = diversified_static_model_weights(
            base, ["model_a", "model_b", MODEL_NAME], "range"
        )
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=12)
        self.assertAlmostEqual(weights[MODEL_NAME], DEFAULT_INITIAL_PRIOR_WEIGHT, places=12)
        self.assertAlmostEqual(weights["model_a"] / weights["model_b"], 1.5, places=12)

    def test_diversified_prior_is_bounded(self) -> None:
        def base(model_names: list[str], _regime: str) -> dict[str, float]:
            return {name: 1.0 / len(model_names) for name in model_names}

        weights = diversified_static_model_weights(
            base,
            ["model_a", "model_b", MODEL_NAME],
            "range",
            initial_weight=0.99,
        )
        self.assertAlmostEqual(weights[MODEL_NAME], MAX_INITIAL_PRIOR_WEIGHT, places=12)
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=12)

    def test_diversified_prior_is_noop_when_model_is_absent(self) -> None:
        calls: list[tuple[list[str], str]] = []

        def base(model_names: list[str], regime: str) -> dict[str, float]:
            calls.append((model_names, regime))
            return {name: 1.0 / len(model_names) for name in model_names}

        weights = diversified_static_model_weights(base, ["model_a", "model_b"], "trending")
        self.assertEqual(calls, [(["model_a", "model_b"], "trending")])
        self.assertEqual(weights, {"model_a": 0.5, "model_b": 0.5})

    def test_augment_baselines_is_research_optional(self) -> None:
        data = make_market()

        def base(_data):
            return {"persistence": {f"{hour}h": {"price_usd": 100.0} for hour in TARGET_HOURS}}

        disabled = augment_baselines(base, data, enabled=False)
        self.assertNotIn(MODEL_NAME, disabled)
        enabled = augment_baselines(base, data, enabled=True)
        self.assertIn(MODEL_NAME, enabled)


if __name__ == "__main__":
    unittest.main()
