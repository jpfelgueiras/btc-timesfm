#!/usr/bin/env python3
"""Unit tests for the weekly walk-forward optimizer."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from unit_test_stubs import install_timesfm_stub

install_timesfm_stub()

import adaptive_weighting as aw  # noqa: E402
import optimizer  # noqa: E402


class OptimizerTests(unittest.TestCase):
    def test_candidate_catalog_is_bounded_and_reproducible(self) -> None:
        first = optimizer.candidate_catalog()
        second = optimizer.candidate_catalog()
        self.assertEqual(first, second)
        self.assertEqual(first[0].name, "production")
        self.assertLessEqual(len(first), 16)
        self.assertGreaterEqual(len(first), 8)
        self.assertEqual(len({item.name for item in first}), len(first))
        for candidate in first:
            self.assertIn("persistence", candidate.enabled_models)

    def test_apply_candidate_restores_production_module_globals(self) -> None:
        original = aw.ADAPTIVE_MAE_LAMBDA
        candidate = optimizer.CandidateConfig(name="test", mae_lambda=9.5)
        with optimizer.apply_candidate(candidate):
            self.assertEqual(aw.ADAPTIVE_MAE_LAMBDA, 9.5)
        self.assertEqual(aw.ADAPTIVE_MAE_LAMBDA, original)

    def test_fold_indices_cover_samples_without_overlap(self) -> None:
        folds = optimizer._fold_indices(10, 3)
        covered = [index for start, end in folds for index in range(start, end)]
        self.assertEqual(covered, list(range(10)))
        self.assertEqual(len(folds), 3)

    def test_replay_hides_future_outcomes_from_weighting(self) -> None:
        samples = [
            {
                "origin_at": "2026-01-01T10:00:00+00:00",
                "origin_timestamp": 1000,
                "current_price": 100.0,
                "regime": "range",
                "model_predictions": {
                    "persistence": {h: {"price_usd": 100.0} for h in ("2h", "4h", "8h", "16h")},
                    "ar1": {h: {"price_usd": 101.0} for h in ("2h", "4h", "8h", "16h")},
                },
                "actuals": {"2h": 102.0, "4h": 103.0, "8h": 104.0, "16h": 105.0},
            },
            {
                "origin_at": "2026-01-01T20:00:00+00:00",
                "origin_timestamp": 2000,
                "current_price": 105.0,
                "regime": "range",
                "model_predictions": {
                    "persistence": {h: {"price_usd": 105.0} for h in ("2h", "4h", "8h", "16h")},
                    "ar1": {h: {"price_usd": 106.0} for h in ("2h", "4h", "8h", "16h")},
                },
                "actuals": {"2h": 106.0, "4h": 107.0, "8h": 108.0, "16h": 109.0},
            },
        ]
        all_actuals = {500: 99.0, 1500: 103.0, 2500: 110.0, 9999: 999.0}
        seen_max_timestamps: list[int] = []

        def fake_weights(model_names, regime, hour, history, actual_by_timestamp, **kwargs):
            seen_max_timestamps.append(max(actual_by_timestamp, default=0))
            weight = 1.0 / len(model_names)
            return ({name: weight for name in model_names}, {"mode": "static_prior"})

        config = optimizer.CandidateConfig(
            name="synthetic",
            enabled_models=("persistence", "ar1"),
        )
        with patch.object(optimizer.aw, "adaptive_model_weights", side_effect=fake_weights):
            optimizer.replay_candidate(config, samples, all_actuals)

        self.assertEqual(seen_max_timestamps[:4], [500] * 4)
        self.assertEqual(seen_max_timestamps[4:], [1500] * 4)
        self.assertNotIn(2500, seen_max_timestamps)
        self.assertNotIn(9999, seen_max_timestamps)

    def _result(self, name: str, mae: float, *, samples: int = 48, direction: float = 0.55,
                horizon_maes: dict[str, float] | None = None,
                fold_maes: tuple[float, float, float] | None = None) -> dict:
        horizons = horizon_maes or {h: mae for h in ("2h", "4h", "8h", "16h")}
        folds = fold_maes or (mae, mae, mae)
        return {
            "name": name,
            "samples": samples,
            "objective_mae_pct": mae,
            "mean_direction_accuracy": direction,
            "by_horizon": {h: {"mae_pct": value} for h, value in horizons.items()},
            "folds": [{"fold": i + 1, "mae_pct": value} for i, value in enumerate(folds)],
        }

    def test_recommendation_accepts_material_stable_improvement(self) -> None:
        current = self._result("production", 1.0, fold_maes=(1.0, 1.0, 1.0))
        candidate = self._result("candidate", 0.94, fold_maes=(0.94, 0.95, 0.93))
        decision, details = optimizer.recommendation(candidate, current)
        self.assertEqual(decision, "candidate_worth_review")
        self.assertTrue(all(details["checks"].values()))

    def test_recommendation_rejects_horizon_regression(self) -> None:
        current = self._result("production", 1.0)
        candidate = self._result(
            "candidate",
            0.94,
            horizon_maes={"2h": 0.8, "4h": 0.8, "8h": 0.8, "16h": 1.08},
            fold_maes=(0.94, 0.94, 0.94),
        )
        decision, details = optimizer.recommendation(candidate, current)
        self.assertEqual(decision, "keep_current")
        self.assertFalse(details["checks"]["no_material_horizon_regression"])

    def test_recommendation_rejects_tiny_sample(self) -> None:
        current = self._result("production", 1.0, samples=20)
        candidate = self._result("candidate", 0.90, samples=20, fold_maes=(0.9, 0.9, 0.9))
        decision, details = optimizer.recommendation(candidate, current)
        self.assertEqual(decision, "keep_current")
        self.assertFalse(details["checks"]["enough_samples"])


if __name__ == "__main__":
    unittest.main()
