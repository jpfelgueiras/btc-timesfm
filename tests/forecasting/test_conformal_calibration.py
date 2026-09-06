#!/usr/bin/env python3
"""Tests for conformal-style interval calibration."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from btc_timesfm.forecasting.conformal_calibration import (
    calibration_details,
    collect_scores,
    evaluation_report,
)


def snapshot(
    origin: datetime,
    *,
    actual: float | None,
    regime: str = "range",
    half_width: float = 10.0,
) -> dict:
    horizons = {}
    outcomes = {}
    for hour in (2, 4, 8, 16):
        horizons[f"{hour}h"] = {
            "price_usd": 100.0,
            "q10_usd": 100.0 - half_width,
            "q90_usd": 100.0 + half_width,
        }
        if actual is not None:
            outcomes[f"{hour}h"] = {"ensemble": {"actual_target_price_usd": actual}}
    return {
        "latest_close_at": origin.isoformat(),
        "regime": regime,
        "predictions": horizons,
        "_outcomes": outcomes,
    }


class ConformalCalibrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def test_sparse_history_uses_legacy_fallback(self) -> None:
        history = [
            snapshot(self.start + timedelta(hours=i), actual=100.0 + (i % 3)) for i in range(8)
        ]
        details = calibration_details(history, {}, 2, min_samples=20)
        self.assertEqual(details["mode"], "legacy_fallback")
        self.assertEqual(details["samples"], 8)
        self.assertEqual(details["multiplier"], 1.0)

    def test_mature_history_uses_finite_sample_conformal_quantile(self) -> None:
        history = [
            snapshot(
                self.start + timedelta(hours=i),
                actual=100.0 + (2.0 if i < 20 else 15.0),
            )
            for i in range(25)
        ]
        details = calibration_details(history, {}, 2, min_samples=20, target_coverage=0.80)
        self.assertEqual(details["mode"], "conformal")
        self.assertEqual(details["samples"], 25)
        self.assertGreaterEqual(details["empirical_coverage_after"], 0.80)
        self.assertIsNotNone(details["average_interval_width_pct_after"])
        self.assertIsNotNone(details["legacy_average_interval_width_pct"])

    def test_regime_history_is_preferred_only_when_not_sparse(self) -> None:
        history = [
            snapshot(
                self.start + timedelta(hours=i),
                actual=101.0,
                regime="trending" if i < 22 else "range",
            )
            for i in range(30)
        ]
        details = calibration_details(history, {}, 2, regime="trending", min_samples=20)
        self.assertEqual(details["source"], "regime")
        self.assertEqual(details["samples"], 22)
        sparse = calibration_details(history, {}, 2, regime="range", min_samples=20)
        self.assertEqual(sparse["source"], "all_regimes")
        self.assertEqual(sparse["samples"], 30)

    def test_unmatured_future_rows_are_ignored(self) -> None:
        history = [snapshot(self.start + timedelta(hours=i), actual=101.0) for i in range(20)]
        history.append(snapshot(self.start + timedelta(days=10), actual=None, half_width=0.01))
        scores = collect_scores(history, {}, 2)
        self.assertEqual(len(scores), 20)
        details = calibration_details(history, {}, 2, min_samples=20)
        self.assertLess(details["multiplier"], 1.0)

    def test_candle_actuals_are_supported_for_walk_forward_callers(self) -> None:
        origin = self.start
        item = snapshot(origin, actual=None)
        actuals = {int((origin + timedelta(hours=2)).timestamp()): 105.0}
        scores = collect_scores([item], actuals, 2)
        self.assertEqual(len(scores), 1)
        self.assertAlmostEqual(scores[0]["score"], 0.5)

    def test_target_coverage_is_configurable_and_validated(self) -> None:
        history = [snapshot(self.start + timedelta(hours=i), actual=101.0) for i in range(25)]
        report = calibration_details(history, {}, 2, target_coverage=0.90, min_samples=20)
        self.assertEqual(report["target_coverage"], 0.90)
        with self.assertRaises(ValueError):
            calibration_details(history, {}, 2, target_coverage=1.0)

    def test_evaluation_report_contains_all_production_horizons(self) -> None:
        history = [snapshot(self.start + timedelta(hours=i), actual=101.0) for i in range(25)]
        report = evaluation_report(history, {}, regime="range")
        self.assertEqual(set(report), {"2h", "4h", "8h", "16h"})
        for value in report.values():
            self.assertIn("legacy_multiplier", value)
            self.assertIn("average_interval_width_pct_after", value)


if __name__ == "__main__":
    unittest.main()
