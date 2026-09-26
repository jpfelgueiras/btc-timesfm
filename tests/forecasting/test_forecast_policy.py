#!/usr/bin/env python3
"""Tests for the immutable shared forecast policy."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

from tests.support.unit_test_stubs import install_timesfm_stub

install_timesfm_stub()

from btc_timesfm.forecasting.forecast_policy import ForecastPolicy, policy_for  # noqa: E402
from btc_timesfm.forecasting.policy_parity import audit_forecast_policy_parity  # noqa: E402


class ForecastPolicyTests(unittest.TestCase):
    def test_identity_is_stable_and_behavior_sensitive(self) -> None:
        first = ForecastPolicy()
        second = ForecastPolicy()
        changed = ForecastPolicy(coherence=False)

        self.assertEqual(first.configuration_id, second.configuration_id)
        self.assertNotEqual(first.configuration_id, changed.configuration_id)

    def test_policy_is_immutable_and_research_variant_isolated(self) -> None:
        production = policy_for("production_forecast")
        research = policy_for("backtest")

        self.assertFalse(production.enable_research_ridge)
        self.assertTrue(research.enable_research_ridge)
        self.assertNotEqual(production.configuration_id, research.configuration_id)
        with self.assertRaises(FrozenInstanceError):
            production.name = "changed"  # type: ignore[misc]

    def test_install_applies_one_idempotent_policy(self) -> None:
        engine = SimpleNamespace(
            adaptive_model_weights=lambda *args: ({}, {}),
            detect_regime=lambda _: "legacy",
            baseline_forecasts=lambda _: {"persistence": {}},
        )
        target = SimpleNamespace(forecast_engine=engine, adaptive_model_weights=None)
        policy = ForecastPolicy()

        policy.install(target)
        original_baselines = engine.baseline_forecasts
        policy.install(target)

        self.assertEqual(engine._forecast_policy_id, policy.configuration_id)
        self.assertIs(engine.baseline_forecasts, original_baselines)
        with self.assertRaisesRegex(RuntimeError, "already has policy"):
            ForecastPolicy(coherence=False).install(target)

    def test_static_parity_audit_fails_closed_and_names_research_ridge(self) -> None:
        audit = audit_forecast_policy_parity()
        self.assertEqual(audit["status"], "blocked")
        self.assertEqual(audit["production_research_ridge_enabled"], False)
        for path in ("backtest", "optimizer"):
            self.assertEqual(audit["paths"][path]["status"], "blocked_policy_drift")
            self.assertTrue(audit["paths"][path]["research_ridge_enabled"])
        self.assertEqual(audit["paths"]["shadow"]["status"], "blocked_unattested")
        self.assertEqual(audit["historical_skill_claim"], "not_made")


if __name__ == "__main__":
    unittest.main()
