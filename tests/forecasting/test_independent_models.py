#!/usr/bin/env python3
"""Tests for the independent forecasting model families (GBDT and elastic-net)."""

from __future__ import annotations

import math
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from btc_timesfm.forecasting.diversified_model import (
    MAX_LAG,
    MODEL_NAME,
    TARGET_HOURS,
    ridge_feature_forecast,
    training_examples,
)
from btc_timesfm.research.independent_models import (
    PRODUCTION_FLAG,
    elasticnet_feature_forecast,
    gbdt_feature_forecast,
    production_enabled,
)


def make_market(count: int = 513) -> SimpleNamespace:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    index: np.ndarray = np.arange(count, dtype=float)
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


def expected_return_limit(data: SimpleNamespace, horizon: int) -> float:
    closes = np.asarray(data.closes, dtype=float)
    recent_volatility = max(float(np.std(np.diff(np.log(closes[-73:])))), 1e-5)
    return max(0.005, 4.0 * recent_volatility * math.sqrt(horizon))


class IndependentModelTests(unittest.TestCase):
    def test_gbdt_schema_is_symmetric_with_ridge(self) -> None:
        data = make_market()
        gbdt = gbdt_feature_forecast(data)
        ridge = ridge_feature_forecast(data)
        self.assertEqual(set(gbdt), set(ridge))
        for horizon, item in gbdt.items():
            self.assertEqual(set(item), {"price_usd", "predicted_log_return", "training_samples"})
            self.assertGreater(item["price_usd"], 0.0)
            self.assertGreater(item["training_samples"], 90)
            self.assertAlmostEqual(item["price_usd"] / ridge[horizon]["price_usd"], 1.0, places=3)

    def test_elasticnet_schema_is_symmetric_with_ridge(self) -> None:
        data = make_market()
        elasticnet = elasticnet_feature_forecast(data)
        ridge = ridge_feature_forecast(data)
        self.assertEqual(set(elasticnet), set(ridge))
        for horizon, item in elasticnet.items():
            self.assertEqual(set(item), {"price_usd", "predicted_log_return", "training_samples"})
            self.assertGreater(item["price_usd"], 0.0)
            self.assertGreater(item["training_samples"], 90)

    def test_forecasters_are_deterministic(self) -> None:
        data = make_market()
        self.assertEqual(gbdt_feature_forecast(data), gbdt_feature_forecast(data))
        self.assertEqual(elasticnet_feature_forecast(data), elasticnet_feature_forecast(data))

    def test_ridge_and_candidates_never_label_beyond_available_window(self) -> None:
        for horizon in TARGET_HOURS:
            _, _, indices = training_examples(make_market(), horizon)
            self.assertGreaterEqual(indices[0], MAX_LAG)
            self.assertLessEqual(indices[-1] + horizon, len(make_market().closes) - 1)

    def test_short_history_is_rejected(self) -> None:
        for forecast in (gbdt_feature_forecast, elasticnet_feature_forecast):
            with self.assertRaises(ValueError):
                forecast(make_market(100))

    def test_gbdt_volatility_clip_guard_bounds_unstable_extrapolation(self) -> None:
        data = make_market()
        with patch(
            "btc_timesfm.research.independent_models._fit_predict_gbdt",
            return_value=5.0,
        ):
            forecast = gbdt_feature_forecast(data)
        for horizon in TARGET_HOURS:
            limit = expected_return_limit(data, horizon)
            self.assertLessEqual(
                abs(forecast[f"{horizon}h"]["predicted_log_return"]), limit + 1e-12
            )

    def test_elasticnet_volatility_clip_guard_bounds_unstable_extrapolation(self) -> None:
        data = make_market()
        with patch(
            "btc_timesfm.research.independent_models._fit_predict_elasticnet",
            return_value=-5.0,
        ):
            forecast = elasticnet_feature_forecast(data)
        for horizon in TARGET_HOURS:
            limit = expected_return_limit(data, horizon)
            self.assertLessEqual(
                abs(forecast[f"{horizon}h"]["predicted_log_return"]), limit + 1e-12
            )

    def test_invalid_hyperparameters_are_rejected(self) -> None:
        data = make_market()
        with self.assertRaises(ValueError):
            gbdt_feature_forecast(data, estimators=0)
        with self.assertRaises(ValueError):
            gbdt_feature_forecast(data, learning_rate=-0.1)
        with self.assertRaises(ValueError):
            elasticnet_feature_forecast(data, alpha=-1.0)
        with self.assertRaises(ValueError):
            elasticnet_feature_forecast(data, l1_ratio=1.5)

    def test_production_model_families_are_opt_in(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(PRODUCTION_FLAG, None)
            self.assertFalse(production_enabled())
        with patch.dict(os.environ, {PRODUCTION_FLAG: "true"}):
            self.assertTrue(production_enabled())

    def test_ridge_model_name_is_distinct_from_candidates(self) -> None:
        self.assertEqual(MODEL_NAME, "ridge_features")
        self.assertNotIn(MODEL_NAME, ("gbdt_features", "elasticnet_features"))


if __name__ == "__main__":
    unittest.main()
