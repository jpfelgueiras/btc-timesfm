#!/usr/bin/env python3
"""Unit tests for backtest benchmark and cross-validation reporting."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from unit_test_stubs import install_timesfm_stub

install_timesfm_stub()

from backtest import evaluate_cross_validation, summarize  # noqa: E402
from benchmarks import BENCHMARK_NAMES  # noqa: E402
from cross_validation import build_purged_walk_forward_folds  # noqa: E402


def horizon_prices(price: float) -> dict[str, dict[str, float]]:
    return {horizon: {"price_usd": price} for horizon in ("2h", "4h", "8h", "16h")}


def make_sample(
    *,
    regime: str,
    current: float,
    actual: float,
    origin_timestamp: int | None = None,
) -> dict:
    if origin_timestamp is None:
        origin_timestamp = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    origin_at = datetime.fromtimestamp(origin_timestamp, tz=timezone.utc).isoformat()
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
        "origin_at": origin_at,
        "origin_timestamp": origin_timestamp,
        "current_price": current,
        "actuals": {horizon: actual for horizon in ("2h", "4h", "8h", "16h")},
        "forecast": {
            "latest_close_at": origin_at,
            "latest_close_usd": current,
            "regime": regime,
            "predictions": predictions,
            "model_predictions": {"persistence": horizon_prices(current)},
            "model_weights": {},
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

    @patch("backtest.adaptive_model_weights")
    def test_cross_validation_reports_fold_metrics_and_dispersion(self, weights) -> None:
        weights.return_value = ({"persistence": 1.0}, {"mode": "test"})
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        samples = [
            make_sample(
                regime="range" if index % 2 == 0 else "trending",
                current=100.0 + index,
                actual=101.0 + index,
                origin_timestamp=int((start + timedelta(days=index)).timestamp()),
            )
            for index in range(9)
        ]
        timestamps = [sample["origin_timestamp"] for sample in samples]
        folds = build_purged_walk_forward_folds(
            timestamps,
            folds=3,
            min_train_samples=3,
            purge_hours=16,
            embargo_hours=2,
        )
        report = evaluate_cross_validation(samples, {}, folds)

        self.assertEqual(len(report["folds"]), 3)
        self.assertEqual(report["objective_mae_pct_across_folds"]["folds"], 3)
        for horizon in ("2h", "4h", "8h", "16h"):
            aggregate = report["aggregate_by_horizon"][horizon]
            self.assertIn("adaptive_ensemble", aggregate)
            self.assertEqual(set(aggregate["benchmarks"]), set(BENCHMARK_NAMES))
            self.assertEqual(report["dispersion_by_horizon"][horizon]["mae_pct"]["folds"], 3)


if __name__ == "__main__":
    unittest.main()
