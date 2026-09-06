#!/usr/bin/env python3
"""Unit tests for paired statistical significance helpers."""

from __future__ import annotations

import unittest

from statistical_significance import paired_bootstrap_comparison


class StatisticalSignificanceTests(unittest.TestCase):
    def test_clear_paired_improvement_is_significant_and_reproducible(self) -> None:
        baseline = [1.0 + (index % 5) * 0.02 for index in range(48)]
        candidate = [value - 0.08 for value in baseline]

        first = paired_bootstrap_comparison(
            candidate,
            baseline,
            metric="mae_pct",
            lower_is_better=True,
        )
        second = paired_bootstrap_comparison(
            candidate,
            baseline,
            metric="mae_pct",
            lower_is_better=True,
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

    def test_clear_regression_supports_baseline(self) -> None:
        result = paired_bootstrap_comparison(
            [1.2] * 40,
            [1.0] * 40,
            metric="mae_pct",
            lower_is_better=True,
        )
        self.assertEqual(result["conclusion"], "baseline_better")
        self.assertLess(result["improvement_ci"]["upper"], 0.0)

    def test_higher_is_better_metric_orientation(self) -> None:
        result = paired_bootstrap_comparison(
            [0.7] * 40,
            [0.5] * 40,
            metric="direction_accuracy",
            lower_is_better=False,
        )
        self.assertEqual(result["conclusion"], "candidate_better")
        self.assertGreater(result["mean_improvement"], 0.0)

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
