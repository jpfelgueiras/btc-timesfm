#!/usr/bin/env python3
"""Tests that production drift state changes adaptive weighting safely."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from tests.support.unit_test_stubs import install_timesfm_stub

install_timesfm_stub()

from btc_timesfm.forecasting.adaptive_weighting import adaptive_model_weights  # noqa: E402
from btc_timesfm.forecasting.forecast_engine import static_model_weights  # noqa: E402


MODELS = ["timesfm_168h", "timesfm_336h", "persistence", "drift_7d", "ar1"]


def history_fixture() -> tuple[list[dict], dict[int, float]]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    history: list[dict] = []
    actuals: dict[int, float] = {}
    for index in range(10):
        origin = start + timedelta(hours=2 * index)
        current = 100.0 + index
        actual = current + 1.0
        history.append(
            {
                "latest_close_at": origin.isoformat(),
                "latest_close_usd": current,
                "regime": "range",
                "model_predictions": {
                    "timesfm_168h": {"2h": {"price_usd": actual + 0.05}},
                    "timesfm_336h": {"2h": {"price_usd": actual + 0.10}},
                    "persistence": {"2h": {"price_usd": current}},
                    "drift_7d": {"2h": {"price_usd": actual + 1.5}},
                    "ar1": {"2h": {"price_usd": actual + 1.0}},
                },
            }
        )
        actuals[int((origin + timedelta(hours=2)).timestamp())] = actual
    return history, actuals


class DriftAdaptiveControlTests(unittest.TestCase):
    def test_severe_drift_falls_back_to_static_prior(self) -> None:
        history, actuals = history_fixture()
        prior = static_model_weights(MODELS, "range")

        weights, diagnostics = adaptive_model_weights(
            MODELS,
            "range",
            2,
            history,
            actuals,
            confidence=0.0,
        )

        self.assertEqual(weights, prior)
        self.assertEqual(diagnostics["mode"], "static_prior")
        self.assertEqual(diagnostics["source"], "drift_fallback")
        self.assertEqual(diagnostics["adaptive_confidence"], 0.0)
        self.assertEqual(diagnostics["blend_factor"], 0.0)

    def test_warning_drift_reduces_learned_weight_blend(self) -> None:
        history, actuals = history_fixture()
        _, normal = adaptive_model_weights(
            MODELS,
            "range",
            2,
            history,
            actuals,
            confidence=1.0,
        )
        _, warning = adaptive_model_weights(
            MODELS,
            "range",
            2,
            history,
            actuals,
            confidence=0.5,
        )

        self.assertEqual(normal["mode"], "adaptive")
        self.assertEqual(warning["mode"], "adaptive")
        self.assertAlmostEqual(
            warning["blend_factor"],
            normal["blend_factor"] * 0.5,
            places=4,
        )
        self.assertEqual(warning["adaptive_confidence"], 0.5)


if __name__ == "__main__":
    unittest.main()
