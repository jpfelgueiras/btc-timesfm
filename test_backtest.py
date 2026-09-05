#!/usr/bin/env python3
"""Unit tests for backtest benchmark reporting."""

from __future__ import annotations

import unittest

from unit_test_stubs import install_timesfm_stub

install_timesfm_stub()

from backtest import summarize  # noqa: E402
from benchmarks import BENCHMARK_NAMES  # noqa: E402


def horizon_prices(price: float) -> dict[str, dict[str, float]]:
    return {
        horizon: {"price_usd": price}
        for horizon in ("2h", "4h", "8h", "16h")
    }


def make_sample(*, regime: str, current: float, actual: float) -> dict:
    predictions = {
        horizon: {
            "price_usd": current + 1.0,
            "q10_usd": current - 5.0,
            "q90_usd": current + 5.0,
        }
        for horizon in ("2h", "4h", "8h", "16h")
    }
    benchmarks = {
        name: horizon_prices(current if name == "persistence" else current + 0.5)
        for name in BENCHMARK_NAMES
    }
    return {
        "current_price": current,
        "actuals": {horizon: actual for horizon in ("2h", "4h", "8h", "16h")},
        "forecast": {
            "latest_close_usd": current,
            "regime": regime,
            "predictions": predictions,
            "model_predictions": {"persistence": horizon_prices(current)},
        },
        "benchmarks": benchmarks,
    }


class BacktestSummaryTests(unittest.TestCase):
    def test_summary_exposes_all_benchmarks_for_every_horizon(self) -> None:
        report = summarize([make_sample(regime="range", current=100.0, actual=102.0)])
        for horizon in ("2h", "4h", "8h", "16h"):
            self.assertEqual(set(report[horizon]["benchmarks"]), set(BENCHMARK_NAMES))
            self.assertIn("persistence", report[horizon]["benchmarks"])
            self.assertIn("benchmark_comparison", report[horizon])

    def test_benchmarks_are_segmented_by_regime(self) -> None:
        report = summarize(
            [
                make_sample(regime="range", current=100.0, actual=101.0),
                make_sample(regime="trending", current=100.0, actual=103.0),
            ]
        )
        per_regime = report["2h"]["benchmarks_by_regime"]
        self.assertEqual(set(per_regime), {"range", "trending"})
        for regime in per_regime:
            self.assertEqual(set(per_regime[regime]), set(BENCHMARK_NAMES))
            self.assertEqual(per_regime[regime]["persistence"]["samples"], 1)

    def test_comparison_reports_best_benchmark_and_persistence_delta(self) -> None:
        report = summarize([make_sample(regime="range", current=100.0, actual=101.0)])
        comparison = report["2h"]["benchmark_comparison"]
        self.assertIn(comparison["best_benchmark"], BENCHMARK_NAMES)
        self.assertIn("adaptive_minus_persistence_mae_pct", comparison)
        self.assertIn("adaptive_minus_best_benchmark_mae_pct", comparison)


if __name__ == "__main__":
    unittest.main()
