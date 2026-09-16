"""Unit tests for deterministic forecast abstention decisions."""

from __future__ import annotations

import unittest

from btc_timesfm.forecasting.abstention_policy import build_abstention_policy


class AbstentionPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.predictions = {
            horizon: {"model_agreement": 0.8} for horizon in ("2h", "4h", "8h", "16h")
        }
        self.drift = {"severity": "none"}
        self.confidence = {"status": "available", "label": "moderate"}
        self.thresholds = {"public_directional_claim_allowed": True}
        self.probability = {"public_claim_allowed": True}

    def build(self, **overrides: object) -> dict:
        inputs = {
            "data_health": {"healthy": True},
            "drift_report": self.drift,
            "forecast_confidence": self.confidence,
            "dynamic_thresholds": self.thresholds,
            "direction_probability": self.probability,
        }
        inputs.update(overrides)
        return build_abstention_policy(self.predictions, **inputs)

    def test_healthy_when_all_evidence_is_usable(self) -> None:
        result = self.build()
        self.assertEqual(result["state"], "healthy")
        self.assertFalse(result["withhold_forecast"])
        self.assertTrue(result["public_directional_claim_allowed"])
        self.assertEqual(result["reasons"], [])

    def test_degraded_data_forces_withholding(self) -> None:
        result = self.build(data_health={"healthy": False})
        self.assertEqual(result["state"], "forecast_withheld")
        self.assertTrue(result["withhold_forecast"])
        self.assertIn("market data health is degraded", result["reasons"])

    def test_severe_drift_forces_withholding(self) -> None:
        result = self.build(drift_report={"severity": "severe"})
        self.assertEqual(result["state"], "forecast_withheld")
        self.assertIn("severe production drift is active", result["reasons"])

    def test_no_measured_edge_suppresses_directional_claims(self) -> None:
        result = self.build(dynamic_thresholds={"public_directional_claim_allowed": False})
        self.assertEqual(result["state"], "low_confidence_no_measurable_edge")
        self.assertFalse(result["withhold_forecast"])
        self.assertFalse(result["public_directional_claim_allowed"])
        self.assertIn("no measurable directional edge across all horizons", result["reasons"])

    def test_disagreement_is_degraded_inputs(self) -> None:
        self.predictions["8h"]["model_agreement"] = 0.4
        result = self.build()
        self.assertEqual(result["state"], "degraded_inputs")
        self.assertIn("8h model agreement 40% is below 50%", result["reasons"])

    def test_recovery_is_deterministic(self) -> None:
        withheld = self.build(data_health={"healthy": False})
        recovered = self.build(data_health={"healthy": True})
        self.assertEqual(withheld["state"], "forecast_withheld")
        self.assertEqual(recovered["state"], "healthy")


if __name__ == "__main__":
    unittest.main()
