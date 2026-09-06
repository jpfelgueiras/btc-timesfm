#!/usr/bin/env python3
"""Tests for champion/challenger interval diagnostics."""

from __future__ import annotations

import unittest

from btc_timesfm.research.champion_challenger_intervals import (
    augment_optimizer_report,
    causal_interval_diagnostic,
)

HORIZONS = ("2h", "4h", "8h", "16h")


class ChampionChallengerIntervalTests(unittest.TestCase):
    def test_causal_diagnostic_uses_only_prior_errors(self) -> None:
        errors = [1.0] * 10 + [2.0, 99.0]
        before_future = causal_interval_diagnostic(errors[:-1])
        with_future = causal_interval_diagnostic(errors)
        self.assertEqual(before_future["evaluated_samples"], 1)
        self.assertEqual(with_future["evaluated_samples"], 2)
        self.assertEqual(
            before_future["mean_calibration_half_width_pct"],
            1.0,
        )
        # The final 99% error is scored from history ending before that error,
        # so it cannot influence its own calibration threshold.
        self.assertEqual(
            with_future["mean_calibration_half_width_pct"],
            1.0,
        )

    def test_augment_adds_coverage_without_overwriting_native_metric(self) -> None:
        errors = [1.0] * 12
        candidate = {
            "name": "production",
            "parameters": {"target_interval_coverage": 0.8},
            "paired_metrics": {"by_horizon": {horizon: list(errors) for horizon in HORIZONS}},
            "by_horizon": {horizon: {"mae_pct": 1.0} for horizon in HORIZONS},
        }
        candidate["by_horizon"]["2h"]["interval_coverage"] = 0.77
        report = augment_optimizer_report({"candidates": [candidate]})
        metrics = report["candidates"][0]["by_horizon"]
        self.assertEqual(metrics["2h"]["interval_coverage"], 0.77)
        self.assertEqual(metrics["4h"]["interval_coverage"], 1.0)
        self.assertEqual(metrics["4h"]["interval_evaluated_samples"], 2)
        diagnostics = report["champion_challenger_interval_diagnostics"]
        self.assertFalse(diagnostics["uses_future_outcomes"])

    def test_sparse_history_is_explicitly_unavailable(self) -> None:
        diagnostic = causal_interval_diagnostic([1.0] * 9)
        self.assertEqual(diagnostic["evaluated_samples"], 0)
        self.assertIsNone(diagnostic["interval_coverage"])
        self.assertIsNone(diagnostic["average_interval_width_pct"])


if __name__ == "__main__":
    unittest.main()
