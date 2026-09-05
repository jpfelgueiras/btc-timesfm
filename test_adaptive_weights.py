#!/usr/bin/env python3
"""Unit tests for adaptive ensemble weighting."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from unit_test_stubs import install_timesfm_stub

install_timesfm_stub()

from forecast_engine import (  # noqa: E402
    ADAPTIVE_MAX_WEIGHT,
    ADAPTIVE_MIN_WEIGHT,
    _bounded_normalize,
    adaptive_model_weights,
    static_model_weights,
)


MODELS = ["timesfm_168h", "timesfm_336h", "persistence", "drift_7d", "ar1"]


def snapshot(
    origin: datetime,
    current: float,
    actual: float,
    *,
    regime: str = "range",
    complex_offset: float | None = None,
) -> tuple[dict, int, float]:
    if complex_offset is None:
        predictions = {
            "timesfm_168h": actual + 0.05,
            "timesfm_336h": actual + 0.10,
            "persistence": current,
            "drift_7d": actual + 1.5,
            "ar1": actual + 1.0,
        }
    else:
        predictions = {
            "timesfm_168h": actual + complex_offset,
            "timesfm_336h": actual + complex_offset,
            "persistence": current,
            "drift_7d": actual + complex_offset,
            "ar1": actual + complex_offset,
        }

    item = {
        "latest_close_at": origin.isoformat(),
        "latest_close_usd": current,
        "regime": regime,
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
        self.assertEqual(diagnostics["source"], "insufficient_history")
        self.assertEqual(weights, prior)

    def test_disabled_adaptation_uses_static_prior(self) -> None:
        prior = static_model_weights(MODELS, "trending")
        weights, diagnostics = adaptive_model_weights(
            MODELS, "trending", 2, [], {}, enabled=False
        )
        self.assertEqual(weights, prior)
        self.assertEqual(diagnostics["mode"], "static_prior")
        self.assertEqual(diagnostics["source"], "disabled")

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
        self.assertEqual(diagnostics["source"], "regime")
        self.assertGreater(weights["timesfm_168h"], weights["drift_7d"])
        self.assertGreater(weights["timesfm_336h"], weights["ar1"])

    def test_all_regimes_are_used_when_current_regime_is_sparse(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        history = []
        actuals = {}
        regimes = ["range"] * 3 + ["trending"] * 5
        for i, regime in enumerate(regimes):
            current = 100.0 + i
            actual = current + 1.0
            item, target, target_price = snapshot(
                start + timedelta(hours=2 * i), current, actual, regime=regime
            )
            history.append(item)
            actuals[target] = target_price

        _, diagnostics = adaptive_model_weights(MODELS, "range", 2, history, actuals)
        self.assertEqual(diagnostics["mode"], "adaptive")
        self.assertEqual(diagnostics["source"], "all_regimes")
        self.assertEqual(diagnostics["sample_count"], len(regimes))

    def test_persistence_fallback_activates_when_it_beats_complex_models(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        history = []
        actuals = {}
        for i in range(10):
            current = 100.0 + i
            actual = current
            item, target, target_price = snapshot(
                start + timedelta(hours=2 * i),
                current,
                actual,
                complex_offset=5.0,
            )
            history.append(item)
            actuals[target] = target_price

        weights, diagnostics = adaptive_model_weights(MODELS, "range", 2, history, actuals)
        self.assertTrue(diagnostics["persistence_fallback"])
        self.assertGreater(weights["persistence"], ADAPTIVE_MIN_WEIGHT)
        self.assertEqual(diagnostics["persistence_mae_pct"], 0.0)

    def test_weights_respect_bounds_and_sum_to_one(self) -> None:
        weights = _bounded_normalize({"a": 100.0, "b": 1.0, "c": 1.0, "d": 1.0})
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=9)
        for value in weights.values():
            self.assertGreaterEqual(value, ADAPTIVE_MIN_WEIGHT - 1e-9)
            self.assertLessEqual(value, ADAPTIVE_MAX_WEIGHT + 1e-9)

    def test_bounded_normalize_rejects_impossible_bounds(self) -> None:
        with self.assertRaises(ValueError):
            _bounded_normalize({"a": 1.0, "b": 1.0}, floor=0.6, cap=0.8)
        with self.assertRaises(ValueError):
            _bounded_normalize({"a": 1.0, "b": 1.0}, floor=0.1, cap=0.4)


if __name__ == "__main__":
    unittest.main()
