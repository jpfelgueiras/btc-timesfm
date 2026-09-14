#!/usr/bin/env python3
"""Tests for the independent model-family walk-forward evaluation report."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from btc_timesfm.research.independent_models_evaluation import (
    PRODUCTION_NOTE,
    build_report,
    render_markdown,
    run_walk_forward_evaluation,
    write_report,
)


def make_market(count: int = 260) -> SimpleNamespace:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    index: np.ndarray = np.arange(count, dtype=float)
    closes = 100.0 * np.exp(0.0003 * index + 0.01 * np.sin(index / 12.0))
    opens = closes * (1.0 - 0.0005)
    highs = closes * (1.0 + 0.002)
    lows = closes * (1.0 - 0.002)
    volumes = 1000.0 + 100.0 * np.sin(index / 7.0) + index * 0.2
    return SimpleNamespace(
        timestamps=[int((start + timedelta(hours=int(i))).timestamp()) for i in index],
        opens=opens,
        highs=highs,
        lows=lows,
        closes=closes,
        volumes=volumes,
    )


class IndependentModelsEvaluationTests(unittest.TestCase):
    def test_walk_forward_origins_are_identical_across_candidates(self) -> None:
        data = make_market()
        samples = run_walk_forward_evaluation(
            data,
            first_origin=190,
            last_origin=243,
            num_origins=8,
        )
        self.assertEqual(len(samples), 8)
        for sample in samples:
            self.assertEqual(
                set(sample["models"]),
                {"gbdt_features", "elasticnet_features", "ridge_features"},
            )
            self.assertEqual(set(sample["runtimes"]), set(sample["models"]))
            self.assertIn("persistence", sample["benchmarks"])
            self.assertIn("regime", sample)
            self.assertEqual(set(sample["actuals"]), {"2h", "4h", "8h", "16h"})

    def test_report_has_per_horizon_metrics_baselines_and_runtime(self) -> None:
        data = make_market()
        samples = run_walk_forward_evaluation(
            data,
            first_origin=190,
            last_origin=243,
            num_origins=8,
        )
        report = build_report(samples, generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(set(report["by_horizon"]), {"2h", "4h", "8h", "16h"})
        self.assertEqual(report["origins"], 8)
        for horizon, block in report["by_horizon"].items():
            models = block["models"]
            self.assertIn("gbdt_features", models)
            self.assertIn("elasticnet_features", models)
            self.assertIn("ridge_features", models)
            self.assertIn("persistence", models)
            for metrics in models.values():
                self.assertEqual(metrics["samples"], 8)
                self.assertIsNotNone(metrics["mae_pct"])
                self.assertIsNotNone(metrics["direction_accuracy"])
            for name in ("gbdt_features", "elasticnet_features", "ridge_features"):
                self.assertIn("mae_delta_vs_persistence_pct", models[name])
                self.assertIn("mae_delta_vs_ridge_pct", models[name])
            self.assertIn("best_candidate_mae", block)
            self.assertIn("candidate_beats_persistence", block)
        for name in ("gbdt_features", "elasticnet_features", "ridge_features"):
            runtime = report["runtime_seconds"][name]
            self.assertEqual(runtime["origins"], 8)
            self.assertGreaterEqual(runtime["mean_seconds_per_forecast"], 0.0)

    def test_report_reports_regime_segmented_skill(self) -> None:
        data = make_market()
        samples = run_walk_forward_evaluation(
            data,
            first_origin=190,
            last_origin=243,
            num_origins=8,
        )
        report = build_report(samples)
        self.assertIn("by_regime", report)
        for regime, block in report["by_regime"].items():
            self.assertGreater(block["samples"], 0)
            for horizon in ("2h", "4h", "8h", "16h"):
                self.assertIn("gbdt_features", block["by_horizon"][horizon])

    def test_report_has_residual_correlation_matrix(self) -> None:
        data = make_market()
        samples = run_walk_forward_evaluation(
            data,
            first_origin=190,
            last_origin=243,
            num_origins=8,
        )
        report = build_report(samples)
        for horizon, block in report["residual_correlation"].items():
            correlation = block["correlation"]
            self.assertIn("gbdt_features", correlation)
            self.assertIn("ridge_features", correlation["gbdt_features"])
            self.assertIn("elasticnet_features", correlation["gbdt_features"])
            self.assertIn("persistence", correlation["gbdt_features"])
            self.assertGreaterEqual(block["pair_samples"]["gbdt_features"]["ridge_features"], 5)
            self.assertIsNotNone(correlation["gbdt_features"]["ridge_features"])
            self.assertEqual(correlation["gbdt_features"]["gbdt_features"], 1.0)

    def test_report_markdown_includes_metrics_correlation_runtime_and_note(self) -> None:
        data = make_market()
        samples = run_walk_forward_evaluation(
            data,
            first_origin=190,
            last_origin=243,
            num_origins=8,
        )
        report = build_report(samples)
        markdown = render_markdown(report)
        self.assertIn("Independent model family evaluation", markdown)
        self.assertIn("gbdt_features", markdown)
        self.assertIn("Residual-error correlation", markdown)
        self.assertIn("Runtime / cost", markdown)
        self.assertIn("Regime breakdown", markdown)
        self.assertIn(PRODUCTION_NOTE, markdown)

    def test_write_report_persists_json_and_markdown(self) -> None:
        data = make_market()
        samples = run_walk_forward_evaluation(
            data,
            first_origin=190,
            last_origin=243,
            num_origins=8,
        )
        report = build_report(samples)
        with tempfile.TemporaryDirectory() as directory:
            json_path = Path(directory) / "report.json"
            markdown_path = Path(directory) / "report.md"
            write_report(report, json_path, markdown_path)
            self.assertTrue(json_path.exists())
            self.assertTrue(markdown_path.exists())
            restored = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(restored["origins"], 8)
            self.assertEqual(restored["production_note"], PRODUCTION_NOTE)

    def test_invalid_origin_bounds_are_rejected(self) -> None:
        data = make_market()
        with self.assertRaises(ValueError):
            run_walk_forward_evaluation(data, first_origin=300, last_origin=250, num_origins=2)


if __name__ == "__main__":
    unittest.main()
