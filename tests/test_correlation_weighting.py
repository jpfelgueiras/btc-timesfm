#!/usr/bin/env python3
"""Tests for residual-correlation-aware ensemble weighting."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from btc_timesfm.forecasting.correlation_weighting import (
    CORRELATION_MIN_SAMPLES,
    correlation_aware_model_weights,
    correlation_penalties,
    residual_correlation_matrix,
    residual_history,
)

MODELS = ["timesfm_168h", "timesfm_336h", "ar1", "persistence"]


def history_rows(
    count: int, *, include_future: bool = False
) -> tuple[list[dict], dict[int, float]]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    actuals: dict[int, float] = {}
    for i in range(count):
        origin = start + timedelta(hours=i)
        actual = 100.0 + i * 0.1
        actuals[int((origin + timedelta(hours=2)).timestamp())] = actual
        common_error = (i % 5 - 2) * 0.15
        model_prices = {
            "timesfm_168h": actual + common_error,
            "timesfm_336h": actual + common_error * 1.02,
            "ar1": actual + (0.35 if i % 2 else -0.30),
            "persistence": 100.0,
        }
        rows.append(
            {
                "latest_close_at": origin.isoformat(),
                "latest_close_usd": 100.0,
                "regime": "range",
                "model_predictions": {
                    name: {"2h": {"price_usd": price}} for name, price in model_prices.items()
                },
                "predictions": {},
            }
        )
    if include_future:
        origin = start + timedelta(days=30)
        rows.append(
            {
                "latest_close_at": origin.isoformat(),
                "latest_close_usd": 100.0,
                "regime": "range",
                "model_predictions": {name: {"2h": {"price_usd": 10000.0}} for name in MODELS},
                "predictions": {},
            }
        )
    return rows, actuals


class CorrelationWeightingTests(unittest.TestCase):
    def test_residual_correlations_use_aligned_matured_origins(self) -> None:
        history, actuals = history_rows(20, include_future=True)
        residuals = residual_history(history, actuals, MODELS, 2)
        correlations, samples = residual_correlation_matrix(residuals)
        self.assertEqual(samples["timesfm_168h"]["timesfm_336h"], 20)
        self.assertGreater(correlations["timesfm_168h"]["timesfm_336h"], 0.99)
        self.assertNotIn(
            (datetime(2026, 1, 31, tzinfo=timezone.utc)).isoformat(),
            residuals["timesfm_168h"],
        )

    def test_sparse_history_leaves_base_policy_unchanged(self) -> None:
        history, actuals = history_rows(CORRELATION_MIN_SAMPLES - 1)
        weights, diagnostics = correlation_aware_model_weights(MODELS, "range", 2, history, actuals)
        self.assertEqual(diagnostics["correlation_mode"], "base_policy")
        self.assertAlmostEqual(sum(weights.values()), 1.0)

    def test_correlated_models_receive_diversification_penalty(self) -> None:
        correlations = {
            "a": {"a": 1.0, "b": 0.99, "c": 0.0},
            "b": {"a": 0.99, "b": 1.0, "c": 0.0},
            "c": {"a": 0.0, "b": 0.0, "c": 1.0},
        }
        samples = {name: {peer: 50 for peer in correlations} for name in correlations}
        penalties, diagnostics = correlation_penalties(
            {"a": 0.35, "b": 0.35, "c": 0.30}, correlations, samples
        )
        self.assertLess(penalties["a"], penalties["c"])
        self.assertLess(penalties["b"], penalties["c"])
        self.assertGreater(
            diagnostics["a"]["redundancy_score"], diagnostics["c"]["redundancy_score"]
        )

    def test_final_weights_respect_existing_bounds(self) -> None:
        history, actuals = history_rows(45)
        weights, diagnostics = correlation_aware_model_weights(MODELS, "range", 2, history, actuals)
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=9)
        for weight in weights.values():
            self.assertGreaterEqual(weight, 0.03 - 1e-9)
            self.assertLessEqual(weight, 0.55 + 1e-9)
        self.assertIn(diagnostics["correlation_mode"], {"active", "base_policy"})

    def test_policy_is_deterministic(self) -> None:
        history, actuals = history_rows(45)
        first = correlation_aware_model_weights(MODELS, "range", 2, history, actuals)
        second = correlation_aware_model_weights(MODELS, "range", 2, history, actuals)
        self.assertEqual(first, second)

    def test_unmatured_future_does_not_change_weights(self) -> None:
        history, actuals = history_rows(45)
        future_history, future_actuals = history_rows(45, include_future=True)
        current = correlation_aware_model_weights(MODELS, "range", 2, history, actuals)
        with_future = correlation_aware_model_weights(
            MODELS, "range", 2, future_history, future_actuals
        )
        self.assertEqual(current, with_future)


if __name__ == "__main__":
    unittest.main()
