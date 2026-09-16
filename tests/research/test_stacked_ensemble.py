#!/usr/bin/env python3
"""Tests for leakage-safe specialist stacking."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from btc_timesfm.research.stacked_ensemble import evaluate_stacked_ensemble

HORIZONS = (2, 4, 8, 16)


def sample(index: int, origin: int) -> dict:
    current = 100.0 + index * 0.1
    features = {
        "volatility_6h_pct": 0.3,
        "volatility_24h_pct": 0.35,
        "volatility_7d_pct": 0.42,
        "momentum_24h_pct": 0.15,
        "rsi_14": 51.0,
    }
    models = {}
    for name, offset in (("timesfm_168h", 0.2), ("ar1", -0.1), ("persistence", 0.0)):
        models[name] = {f"{hour}h": {"price_usd": current + offset} for hour in HORIZONS}
    return {
        "origin_timestamp": origin,
        "current_price": current,
        "actuals": {f"{hour}h": current + 0.15 for hour in HORIZONS},
        "forecast": {
            "latest_close_at": datetime.fromtimestamp(origin, tz=timezone.utc).isoformat(),
            "latest_close_usd": current,
            "regime": "range",
            "market_features": features,
            "model_predictions": models,
            "predictions": {},
        },
    }


class StackedEnsembleTests(unittest.TestCase):
    def test_uses_purged_oof_folds_and_reports_comparisons(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        samples = [
            sample(index, int((start + timedelta(hours=index * 2)).timestamp()))
            for index in range(28)
        ]
        actuals = {
            entry["origin_timestamp"] + hour * 3600: float(entry["actuals"][f"{hour}h"])
            for entry in samples
            for hour in HORIZONS
        }
        report = evaluate_stacked_ensemble(samples, actuals, min_train_samples=12)
        self.assertEqual(report["specialists"], ["production", "horizon", "regime"])
        self.assertEqual(set(report["by_horizon"]), {"2h", "4h", "8h", "16h"})
        self.assertGreater(len(report["folds"]), 0)
        for fold in report["folds"]:
            self.assertLess(fold["train_end_at"], fold["validation_start_at"])
            self.assertEqual(set(fold["weights"]["2h"]), {"production", "horizon", "regime"})
            self.assertAlmostEqual(sum(fold["weights"]["2h"].values()), 1.0, places=5)
        comparison = report["by_horizon"]["2h"]["vs_production"]
        self.assertEqual(report["experiment_manifest"]["promotion_mode"], "shadow_only")
        self.assertGreater(comparison["stack"]["samples"], 0)
        self.assertIn("improvement_ci", comparison["comparison"])

    def test_future_actual_does_not_change_result(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        samples = [
            sample(index, int((start + timedelta(hours=index * 2)).timestamp()))
            for index in range(28)
        ]
        actuals = {
            entry["origin_timestamp"] + hour * 3600: float(entry["actuals"][f"{hour}h"])
            for entry in samples
            for hour in HORIZONS
        }
        first = evaluate_stacked_ensemble(samples, actuals, min_train_samples=12)
        altered = dict(actuals)
        altered[max(altered) + 3600] = 1_000_000.0
        self.assertEqual(first, evaluate_stacked_ensemble(samples, altered, min_train_samples=12))

    def test_manifest_id_changes_when_evaluated_input_changes(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        samples = [
            sample(index, int((start + timedelta(hours=index * 2)).timestamp()))
            for index in range(28)
        ]
        actuals = {
            entry["origin_timestamp"] + hour * 3600: float(entry["actuals"][f"{hour}h"])
            for entry in samples
            for hour in HORIZONS
        }
        first = evaluate_stacked_ensemble(samples, actuals, min_train_samples=12)
        samples[-1]["actuals"]["2h"] += 1.0
        second = evaluate_stacked_ensemble(samples, actuals, min_train_samples=12)
        self.assertNotEqual(
            first["experiment_manifest"]["data_id"], second["experiment_manifest"]["data_id"]
        )
        self.assertNotEqual(
            first["experiment_manifest"]["run_id"], second["experiment_manifest"]["run_id"]
        )


if __name__ == "__main__":
    unittest.main()
