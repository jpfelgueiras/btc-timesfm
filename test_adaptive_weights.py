#!/usr/bin/env python3
"""Unit tests for adaptive ensemble weighting."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from forecast_engine import (
    ADAPTIVE_MAX_WEIGHT,
    ADAPTIVE_MIN_WEIGHT,
    _bounded_normalize,
    adaptive_model_weights,
    static_model_weights,
)


MODELS = ["timesfm_168h", "timesfm_336h", "persistence", "drift_7d", "ar1"]


def snapshot(origin: datetime, current: float, actual: float) -> tuple[dict, int, float]:
    predictions = {
        "timesfm_168h": actual + 0.05,
        "timesfm_336h": actual + 0.10,
        "persistence": current,
        "drift_7d": actual + 1.5,
        "ar1": actual + 1.0,
    }
    item = {
        "latest_close_at": origin.isoformat(),
        "latest_close_usd": current,
        "regime": "range",
        "model_predictions": {
            name: {"2h": {"price_usd": price}} for name, price in predictions.items()
        },
    }
    return item, int((origin + timedelta(hours=2)).timestamp()), actual


class AdaptiveWeightTests(unittest.TestCase):
    def test_sparse_history_uses_static_prior(self) -> None:
        prior = static_model_weights(MODELS, "range")
        weights, diagnostics = adaptive_model_weights(MODELS, "range", 2, [], {})
        self.assertEqual(diagnostics["mode"], "static_prior")
        self.assertEqual(weights, prior)

    def test_better_models_receive_more_weight(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        history = []
        actuals = {}
        for i in range(12):
            current = 100.0 + i
            actual = current + 1.0
            item, target, target_price = snapshot(start + timedelta(hours=2 * i), current, actual)
            history.append(item)
            actuals[target] = target_price

        weights, diagnostics = adaptive_model_weights(MODELS, "range", 2, history, actuals)
        self.assertEqual(diagnostics["mode"], "adaptive")
        self.assertGreater(weights["timesfm_168h"], weights["drift_7d"])
        self.assertGreater(weights["timesfm_336h"], weights["ar1"])

    def test_weights_respect_bounds_and_sum_to_one(self) -> None:
        weights = _bounded_normalize({"a": 100.0, "b": 1.0, "c": 1.0, "d": 1.0})
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=9)
        for value in weights.values():
            self.assertGreaterEqual(value, ADAPTIVE_MIN_WEIGHT - 1e-9)
            self.assertLessEqual(value, ADAPTIVE_MAX_WEIGHT + 1e-9)


if __name__ == "__main__":
    unittest.main()
