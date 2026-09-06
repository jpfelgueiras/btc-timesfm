#!/usr/bin/env python3
"""Unit tests for adaptive ensemble weighting."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from tests.support.unit_test_stubs import install_timesfm_stub

install_timesfm_stub()

from btc_timesfm.forecasting.adaptive_weighting import (  # noqa: E402
    attach_persisted_outcomes,
    adaptive_model_weights,
)
from btc_timesfm.forecasting.forecast_engine import (  # noqa: E402
    ADAPTIVE_MAX_WEIGHT,
    ADAPTIVE_MIN_WEIGHT,
    _bounded_normalize,
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
    with_intervals: bool = False,
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

    model_predictions = {}
    for name, price in predictions.items():
        item = {"price_usd": price}
        if with_intervals and name.startswith("timesfm_"):
            item.update({"q10_usd": actual - 1.0, "q90_usd": actual + 1.0})
        model_predictions[name] = {"2h": item}

    item = {
        "latest_close_at": origin.isoformat(),
        "latest_close_usd": current,
        "regime": regime,
        "model_predictions": model_predictions,
    }
    return item, int((origin + timedelta(hours=2)).timestamp()), actual


def add_durable_outcome(item: dict, actual: float) -> None:
    item["_outcomes"] = {
        "2h": {
            name: {"actual_target_price_usd": actual, "matured_at": "2026-01-02T00:00:00+00:00"}
            for name in MODELS
        }
    }


class AdaptiveWeightTests(unittest.TestCase):
    def test_sparse_history_uses_static_prior(self) -> None:
        prior = static_model_weights(MODELS, "range")
        weights, diagnostics = adaptive_model_weights(MODELS, "range", 2, [], {})
        self.assertEqual(diagnostics["mode"], "static_prior")
        self.assertEqual(diagnostics["source"], "insufficient_history")
        self.assertEqual(weights, prior)

    def test_disabled_adaptation_uses_static_prior(self) -> None:
        prior = static_model_weights(MODELS, "trending")
        weights, diagnostics = adaptive_model_weights(MODELS, "trending", 2, [], {}, enabled=False)
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

    def test_durable_outcomes_work_without_recent_candle_map(self) -> None:
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        history = []
        for i in range(8):
            current = 100.0 + i
            actual = current + 1.0
            item, _, _ = snapshot(start + timedelta(hours=2 * i), current, actual)
            add_durable_outcome(item, actual)
            history.append(item)

        _, diagnostics = adaptive_model_weights(MODELS, "range", 2, history, {})
        self.assertEqual(diagnostics["mode"], "adaptive")
        self.assertEqual(diagnostics["sample_count"], 8)
        for model in MODELS:
            self.assertEqual(diagnostics["models"][model]["durable_outcome_samples"], 8)
            self.assertEqual(diagnostics["models"][model]["candle_outcome_samples"], 0)

    def test_history_limit_applies_after_regime_filtering(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        history = []
        actuals = {}
        for i in range(20):
            regime = "range" if i % 2 == 0 else "trending"
            current = 100.0 + i
            actual = current + 1.0
            item, target, target_price = snapshot(
                start + timedelta(hours=2 * i), current, actual, regime=regime
            )
            history.append(item)
            actuals[target] = target_price

        _, diagnostics = adaptive_model_weights(
            MODELS, "range", 2, history, actuals, history_limit=7
        )
        self.assertEqual(diagnostics["source"], "regime")
        self.assertEqual(diagnostics["sample_count"], 7)
        self.assertEqual(diagnostics["history_limit"], 7)

    def test_interval_coverage_is_measured_and_affects_score(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        history = []
        actuals = {}
        for i in range(10):
            current = 100.0 + i
            actual = current + 1.0
            item, target, target_price = snapshot(
                start + timedelta(hours=2 * i), current, actual, with_intervals=True
            )
            # Give both TimesFM contexts identical point forecasts so coverage is
            # the only difference between their raw performance scores.
            item["model_predictions"]["timesfm_168h"]["2h"]["price_usd"] = actual + 0.1
            item["model_predictions"]["timesfm_336h"]["2h"]["price_usd"] = actual + 0.1
            # 168h gets 8/10 ~= target 80%; 336h misses all intervals.
            if i >= 8:
                item["model_predictions"]["timesfm_168h"]["2h"].update(
                    {"q10_usd": actual + 2.0, "q90_usd": actual + 3.0}
                )
            item["model_predictions"]["timesfm_336h"]["2h"].update(
                {"q10_usd": actual + 2.0, "q90_usd": actual + 3.0}
            )
            history.append(item)
            actuals[target] = target_price

        _, diagnostics = adaptive_model_weights(MODELS, "range", 2, history, actuals)
        m168 = diagnostics["models"]["timesfm_168h"]
        m336 = diagnostics["models"]["timesfm_336h"]
        self.assertEqual(m168["interval_samples"], 10)
        self.assertAlmostEqual(m168["q10_q90_coverage"], 0.8)
        self.assertEqual(m336["q10_q90_coverage"], 0.0)
        self.assertGreater(m168["raw_score"], m336["raw_score"])

    def test_attach_persisted_outcomes_maps_rows_by_origin_model_and_horizon(self) -> None:
        origin = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
        snapshots = [{"latest_close_at": origin}]
        rows = [
            {
                "origin_at": origin,
                "model_name": "timesfm_168h",
                "horizon_hours": 2,
                "actual_target_price_usd": 101.0,
                "absolute_error_pct": 0.2,
                "signed_error_pct": -0.2,
                "direction_correct": 1,
                "within_q10_q90": 1,
                "matured_at": "2026-01-01T03:00:00+00:00",
            },
            {
                "origin_at": origin,
                "model_name": "ensemble",
                "horizon_hours": 2,
                "actual_target_price_usd": 101.0,
            },
        ]
        result = attach_persisted_outcomes(snapshots, rows)
        self.assertEqual(
            result[0]["_outcomes"]["2h"]["timesfm_168h"]["actual_target_price_usd"],
            101.0,
        )
        self.assertNotIn("ensemble", result[0]["_outcomes"]["2h"])

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
