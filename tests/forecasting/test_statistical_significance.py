#!/usr/bin/env python3
"""Unit tests for paired statistical significance helpers."""

from __future__ import annotations

import unittest

import numpy as np

from btc_timesfm.forecasting.statistical_significance import paired_bootstrap_comparison


class StatisticalSignificanceTests(unittest.TestCase):
    def test_clear_paired_improvement_is_significant_and_reproducible(self) -> None:
        baseline = [1.0 + (index % 5) * 0.02 for index in range(48)]
        candidate = [value - 0.08 for value in baseline]

        first = paired_bootstrap_comparison(
            candidate,
            baseline,
            metric="mae_pct",
            lower_is_better=True,
            min_effective_samples=1,
        )
        second = paired_bootstrap_comparison(
            candidate,
            baseline,
            metric="mae_pct",
            lower_is_better=True,
            min_effective_samples=1,
        )

        self.assertEqual(first, second)
        self.assertEqual(first["samples"], 48)
        self.assertEqual(first["conclusion"], "candidate_better")
        self.assertGreater(first["improvement_ci"]["lower"], 0.0)
        self.assertGreater(first["relative_effect_size"], 0.0)

    def test_low_sample_result_is_explicitly_inconclusive(self) -> None:
        result = paired_bootstrap_comparison(
            [0.8] * 12,
            [1.0] * 12,
            metric="mae_pct",
            lower_is_better=True,
        )
        self.assertEqual(result["conclusion"], "inconclusive")
        self.assertEqual(result["reason"], "insufficient_samples")
        self.assertEqual(result["samples"], 12)

    def test_dependence_default_blocks_and_gates_on_effective_samples(self) -> None:
        baseline = [1.0 + (index % 3) * 0.01 for index in range(48)]
        candidate = [value - 0.02 for value in baseline]
        result = paired_bootstrap_comparison(
            candidate,
            baseline,
            metric="mae_pct",
            lower_is_better=True,
            block_length=16,
        )
        self.assertEqual(result["bootstrap_method"], "moving_block")
        self.assertEqual(result["effective_samples"], 3.0)
        self.assertEqual(result["conclusion"], "inconclusive")
        self.assertEqual(result["reason"], "insufficient_effective_samples")

    def test_bounded_monte_carlo_null_coverage_and_power(self) -> None:
        # Fixed seed, 240 replications, 399 bootstrap draws each: bounded and
        # reproducible (49 million resampled observations per condition).
        # The 16-wide moving average simulates overlapping 16h losses. Block
        # length is fixed at 16 before each simulated evaluation path.
        rng = np.random.default_rng(342)
        replications = 240
        observations = 512
        bootstrap_draws = 399
        effect = 0.08
        null_rejections = 0
        effect_coverages = 0
        effect_rejections = 0
        for _ in range(replications):
            innovations = rng.normal(size=observations + 15)
            correlated = np.convolve(innovations, np.ones(16) / 4.0, mode="valid")
            null = paired_bootstrap_comparison(
                correlated,
                np.zeros(observations),
                metric="synthetic_null",
                lower_is_better=True,
                iterations=bootstrap_draws,
                min_samples=1,
                min_effective_samples=1,
                block_length=16,
            )
            alternative = paired_bootstrap_comparison(
                correlated - effect,
                np.zeros(observations),
                metric="synthetic_effect",
                lower_is_better=True,
                iterations=bootstrap_draws,
                min_samples=1,
                min_effective_samples=1,
                block_length=16,
            )
            null_ci = null["improvement_ci"]
            effect_ci = alternative["improvement_ci"]
            null_rejections += int(null_ci["lower"] > 0.0 or null_ci["upper"] < 0.0)
            effect_coverages += int(effect_ci["lower"] <= effect <= effect_ci["upper"])
            effect_rejections += int(effect_ci["lower"] > 0.0)

        null_rate = null_rejections / replications
        coverage = effect_coverages / replications
        power = effect_rejections / replications
        self.assertGreaterEqual(null_rate, 0.005, f"empirical null rejection rate={null_rate:.3f}")
        self.assertLessEqual(null_rate, 0.10, f"empirical null rejection rate={null_rate:.3f}")
        self.assertGreaterEqual(coverage, 0.87, f"effect CI coverage={coverage:.3f}")
        self.assertLessEqual(coverage, 0.995, f"effect CI coverage={coverage:.3f}")
        self.assertGreaterEqual(power, 0.20, f"empirical power={power:.3f}")
        self.assertLessEqual(power, 0.90, f"empirical power={power:.3f}")

    def test_clear_regression_supports_baseline(self) -> None:
        result = paired_bootstrap_comparison(
            [1.2] * 40,
            [1.0] * 40,
            metric="mae_pct",
            lower_is_better=True,
            min_effective_samples=1,
        )
        self.assertEqual(result["conclusion"], "baseline_better")
        self.assertLess(result["improvement_ci"]["upper"], 0.0)

    def test_higher_is_better_metric_orientation(self) -> None:
        result = paired_bootstrap_comparison(
            [0.7] * 40,
            [0.5] * 40,
            metric="direction_accuracy",
            lower_is_better=False,
            min_effective_samples=1,
        )
        self.assertEqual(result["conclusion"], "candidate_better")
        self.assertGreater(result["mean_improvement"], 0.0)

    def test_block_bootstrap_is_deterministic_and_reports_effective_samples(self) -> None:
        baseline = [1.0 + (index % 8) * 0.02 for index in range(64)]
        candidate = [value - 0.04 for value in baseline]
        first = paired_bootstrap_comparison(
            candidate,
            baseline,
            metric="mae_pct",
            lower_is_better=True,
            method="moving_block",
            block_length=16,
            min_effective_samples=1,
        )
        second = paired_bootstrap_comparison(
            candidate,
            baseline,
            metric="mae_pct",
            lower_is_better=True,
            method="moving_block",
            block_length=16,
            min_effective_samples=1,
        )
        self.assertEqual(first, second)
        self.assertEqual(first["block_length"], 16)
        self.assertEqual(first["effective_samples"], 4.0)
        self.assertEqual(first["effective_block_count_proxy"], 4.0)

    def test_default_block_floor_covers_overlapping_16h_labels(self) -> None:
        result = paired_bootstrap_comparison(
            [1.0] * 512,
            [1.0] * 512,
            metric="mae_pct",
            lower_is_better=True,
            min_effective_samples=1,
        )
        self.assertGreaterEqual(result["block_length"], 16)
        self.assertEqual(result["block_length_basis"], "max_overlap_floor_cube_root")

    def test_unpaired_sample_counts_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "identical sample counts"):
            paired_bootstrap_comparison(
                [1.0, 2.0],
                [1.0],
                metric="mae_pct",
                lower_is_better=True,
            )


if __name__ == "__main__":
    unittest.main()
