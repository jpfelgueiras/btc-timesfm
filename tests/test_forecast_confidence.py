#!/usr/bin/env python3
"""Unit tests for statistically grounded forecast confidence diagnostics."""

from __future__ import annotations

import unittest

from btc_timesfm.forecasting.forecast_confidence import (
    build_forecast_confidence,
    horizon_confidence,
)


def _prediction(width_pct: float = 2.0) -> dict:
    point = 100.0
    half = point * width_pct / 200.0
    return {
        "price_usd": point,
        "q10_usd": point - half,
        "q90_usd": point + half,
    }


def _performance(
    *, samples: int = 50, ensemble_mae: float = 0.8, persistence_mae: float = 1.0
) -> dict:
    return {
        "samples": samples,
        "mae_pct": ensemble_mae,
        "direction_accuracy": 0.62,
        "models": {"persistence": {"samples": samples, "mae_pct": persistence_mae}},
    }


def _calibration(*, samples: int = 50, coverage: float = 0.80, width_pct: float = 2.0) -> dict:
    return {
        "mode": "conformal",
        "source": "all_regimes",
        "samples": samples,
        "target_coverage": 0.80,
        "empirical_coverage_after": coverage,
        "average_interval_width_pct_after": width_pct,
    }


def _drift(severity: str = "none", adaptive_confidence: float = 1.0) -> dict:
    return {"severity": severity, "adaptive_confidence": adaptive_confidence}


class ForecastConfidenceTests(unittest.TestCase):
    def test_strong_oos_evidence_can_reach_high_band(self) -> None:
        result = horizon_confidence(
            "2h",
            _prediction(),
            _performance(),
            _calibration(),
            _drift(),
        )
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["label"], "high")
        self.assertGreaterEqual(result["score"], 70.0)
        self.assertAlmostEqual(result["edge_vs_persistence_pct"], 20.0)
        self.assertEqual(result["evidence_samples"], 50)

    def test_low_sample_history_suppresses_confidence_claim(self) -> None:
        result = horizon_confidence(
            "16h",
            _prediction(),
            _performance(samples=12),
            _calibration(samples=12),
            _drift(),
        )
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertIsNone(result["label"])
        self.assertIsNone(result["score"])
        self.assertTrue(any("at least 20" in reason for reason in result["reasons"]))

    def test_failure_to_beat_persistence_caps_band_at_low(self) -> None:
        result = horizon_confidence(
            "4h",
            _prediction(),
            _performance(ensemble_mae=1.1, persistence_mae=1.0),
            _calibration(),
            _drift(),
        )
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["label"], "low")
        self.assertLess(result["edge_vs_persistence_pct"], 0.0)
        self.assertLess(result["score"], 45.0)

    def test_warning_drift_reduces_and_caps_confidence(self) -> None:
        clean = horizon_confidence("8h", _prediction(), _performance(), _calibration(), _drift())
        warning = horizon_confidence(
            "8h",
            _prediction(),
            _performance(),
            _calibration(),
            _drift("warning", 0.60),
        )
        self.assertEqual(warning["status"], "available")
        self.assertLess(warning["score"], clean["score"])
        self.assertNotEqual(warning["label"], "high")
        self.assertTrue(any("drift" in reason for reason in warning["reasons"]))

    def test_severe_drift_suppresses_confidence(self) -> None:
        result = horizon_confidence(
            "2h",
            _prediction(),
            _performance(),
            _calibration(),
            _drift("severe", 0.0),
        )
        self.assertEqual(result["status"], "suppressed_drift")
        self.assertIsNone(result["label"])
        self.assertIsNone(result["score"])

    def test_unusually_wide_interval_reduces_score(self) -> None:
        normal = horizon_confidence(
            "2h", _prediction(2.0), _performance(), _calibration(width_pct=2.0), _drift()
        )
        wide = horizon_confidence(
            "2h", _prediction(5.0), _performance(), _calibration(width_pct=2.0), _drift()
        )
        self.assertLess(wide["score"], normal["score"])
        self.assertLess(
            wide["factors"]["interval_informativeness"],
            normal["factors"]["interval_informativeness"],
        )

    def test_missing_persistence_baseline_is_insufficient(self) -> None:
        performance = _performance()
        performance["models"] = {}
        result = horizon_confidence("2h", _prediction(), performance, _calibration(), _drift())
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertTrue(any("persistence" in reason for reason in result["reasons"]))

    def test_overall_band_is_bounded_by_weakest_horizon(self) -> None:
        predictions = {h: _prediction() for h in ("2h", "4h", "8h", "16h")}
        performance = {h: _performance() for h in predictions}
        calibration = {h: _calibration() for h in predictions}
        performance["16h"] = _performance(ensemble_mae=1.1, persistence_mae=1.0)

        result = build_forecast_confidence(predictions, performance, calibration, _drift())
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["label"], "low")
        self.assertLess(result["score"], 45.0)
        self.assertEqual(result["minimum_evidence_samples"], 50)
        self.assertLess(result["minimum_edge_vs_persistence_pct"], 0.0)

    def test_overall_requires_all_four_horizons(self) -> None:
        predictions = {h: _prediction() for h in ("2h", "4h", "8h", "16h")}
        performance = {h: _performance() for h in predictions}
        calibration = {h: _calibration() for h in predictions}
        calibration["16h"] = _calibration(samples=10)

        result = build_forecast_confidence(predictions, performance, calibration, _drift())
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertIsNone(result["label"])
        self.assertIsNone(result["score"])


if __name__ == "__main__":
    unittest.main()
