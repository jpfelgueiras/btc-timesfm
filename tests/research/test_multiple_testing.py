#!/usr/bin/env python3
"""Unit tests for multiple-testing safeguards."""

from __future__ import annotations

import copy
import unittest
from typing import Any

from btc_timesfm.forecasting.statistical_significance import (
    paired_bootstrap_comparison,
)
from btc_timesfm.research.multiple_testing import (
    POLICY_VERSION,
    VALID_METHODS,
    MultipleTestingPolicy,
    adjusted_bootstrap_comparison,
    adjusted_p_value,
    build_multiple_testing_assessment,
    default_policy,
    family_comparison_count,
    family_rank,
    harmonic_number,
    per_comparison_alpha,
    policy_from_dict,
    policy_identity,
    policy_to_dict,
    reject_family,
)

HORIZONS = ("2h", "4h", "8h", "16h")


def _candidate(name: str, mae: float, coverage: float) -> dict[str, Any]:
    origins = [
        "2026-01-01T00:00:00+00:00",
        "2026-01-02T00:00:00+00:00",
        "2026-01-03T00:00:00+00:00",
    ]
    by_horizon = {
        horizon: {
            "samples": 3,
            "mae_pct": mae,
            "mean_signed_error_pct": mae / 10,
            "direction_accuracy": 2 / 3,
            "interval_coverage": coverage,
            "average_interval_width_pct": 2.5,
        }
        for horizon in HORIZONS
    }
    return {
        "name": name,
        "parameters": {"history_limit": 200 if name == "production" else 300},
        "samples": 3,
        "objective_mae_pct": mae,
        "mean_direction_accuracy": 2 / 3,
        "by_horizon": by_horizon,
        "by_regime": {"range": copy.deepcopy(by_horizon)},
        "persistence_by_horizon": {horizon: {"samples": 3, "mae_pct": 1.2} for horizon in HORIZONS},
        "folds": [
            {"fold": 1, "mae_pct": mae},
            {"fold": 2, "mae_pct": mae + 0.01},
            {"fold": 3, "mae_pct": mae - 0.01},
        ],
        "paired_metrics": {
            "origins": origins,
            "mae_pct": [mae, mae, mae],
            "direction_accuracy": [1.0, 0.0, 1.0],
            "by_horizon": {horizon: [mae, mae, mae] for horizon in HORIZONS},
            "persistence_mae_pct": [1.2, 1.2, 1.2],
        },
    }


def _optimizer_report(n_challengers: int = 3, champion_mae: float = 1.0) -> dict[str, Any]:
    production = _candidate("production", champion_mae, 0.80)
    challengers = [
        _candidate(f"variant_{i}", champion_mae - 0.01 * (i + 1), 0.82)
        for i in range(n_challengers)
    ]
    return {
        "schema_version": 3,
        "generated_at": "2026-01-04T00:00:00+00:00",
        "data_source": "synthetic",
        "tested_period": {"days": 30, "samples": 3},
        "candidates": [production] + challengers,
        "comparison": {
            "relative_mae_improvement": 0.01,
            "significance": {
                "candidate_vs_production": {
                    "mae_pct": {"conclusion": "candidate_better"},
                },
                "candidate_vs_persistence": {
                    "mae_pct": {"conclusion": "candidate_better"},
                },
            },
        },
    }


class PolicyConfigTests(unittest.TestCase):
    def test_default_policy_is_valid(self) -> None:
        policy = default_policy()
        self.assertEqual(policy.method, "holm")
        self.assertEqual(policy.alpha, 0.05)
        self.assertEqual(policy.max_comparisons, 1000)

    def test_all_valid_methods_accepted(self) -> None:
        for method in VALID_METHODS:
            policy = MultipleTestingPolicy(method=method)
            self.assertEqual(policy.method, method)

    def test_invalid_method_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid method"):
            MultipleTestingPolicy(method="unknown")

    def test_alpha_out_of_range_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be between"):
            MultipleTestingPolicy(alpha=1.5)

    def test_max_comparisons_must_be_positive(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive integer"):
            MultipleTestingPolicy(max_comparisons=0)

    def test_policy_roundtrip_through_dict(self) -> None:
        policy = MultipleTestingPolicy(method="bonferroni", alpha=0.01, max_comparisons=50)
        data = policy_to_dict(policy)
        restored = policy_from_dict(data)
        self.assertEqual(restored.method, "bonferroni")
        self.assertAlmostEqual(restored.alpha, 0.01)
        self.assertEqual(restored.max_comparisons, 50)

    def test_policy_from_dict_missing_fields_uses_defaults(self) -> None:
        restored = policy_from_dict({})
        self.assertEqual(restored.method, "holm")
        self.assertEqual(restored.alpha, 0.05)

    def test_policy_identity_depends_on_method(self) -> None:
        a = policy_identity(MultipleTestingPolicy(method="holm"))
        b = policy_identity(MultipleTestingPolicy(method="bonferroni"))
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("multiple-testing-policy-"))

    def test_policy_to_dict_contains_version(self) -> None:
        data = policy_to_dict(default_policy())
        self.assertEqual(data["version"], POLICY_VERSION)


class FamilyCountingTests(unittest.TestCase):
    def test_basic_count(self) -> None:
        result = family_comparison_count(5, policy=default_policy())
        self.assertEqual(result["comparisons_considered"], 5)
        self.assertFalse(result["truncated"])

    def test_truncated_at_max(self) -> None:
        policy = MultipleTestingPolicy(max_comparisons=10)
        result = family_comparison_count(20, policy=policy)
        self.assertEqual(result["comparisons_considered"], 10)
        self.assertTrue(result["truncated"])

    def test_none_defaults_to_one(self) -> None:
        result = family_comparison_count(None, policy=default_policy())
        self.assertEqual(result["raw_count"], 1)
        self.assertEqual(result["comparisons_considered"], 1)

    def test_family_name_recorded(self) -> None:
        result = family_comparison_count(3, policy=default_policy(), family="test_family")
        self.assertEqual(result["family"], "test_family")


class PerComparisonAlphaTests(unittest.TestCase):
    def test_bonferroni(self) -> None:
        alpha = per_comparison_alpha("bonferroni", 0.05, 10)
        self.assertAlmostEqual(alpha, 0.005)

    def test_sidak(self) -> None:
        alpha = per_comparison_alpha("sidak", 0.05, 10)
        self.assertAlmostEqual(alpha, 1.0 - (1.0 - 0.05) ** 0.1, places=8)

    def test_holm_first_rank(self) -> None:
        alpha = per_comparison_alpha("holm", 0.05, 10, rank=1)
        self.assertAlmostEqual(alpha, 0.05 / 10)

    def test_holm_last_rank(self) -> None:
        alpha = per_comparison_alpha("holm", 0.05, 10, rank=10)
        self.assertAlmostEqual(alpha, 0.05)

    def test_by_smallest_family(self) -> None:
        alpha = per_comparison_alpha("by", 0.05, 1, rank=1)
        self.assertAlmostEqual(alpha, 0.05)

    def test_single_comparison_identity(self) -> None:
        for method in VALID_METHODS:
            alpha = per_comparison_alpha(method, 0.05, 1)
            self.assertAlmostEqual(alpha, 0.05, places=6, msg=f"method={method}")


class AdjustedPValueTests(unittest.TestCase):
    def test_bonferroni_multiplication(self) -> None:
        adjusted = adjusted_p_value("bonferroni", 0.01, 5)
        self.assertAlmostEqual(adjusted, 0.05)

    def test_bonferroni_capped_at_one(self) -> None:
        adjusted = adjusted_p_value("bonferroni", 0.5, 5)
        self.assertEqual(adjusted, 1.0)

    def test_holm_matches_bonferroni_for_first_rank(self) -> None:
        b = adjusted_p_value("bonferroni", 0.01, 5)
        h = adjusted_p_value("holm", 0.01, 5, rank=1)
        self.assertAlmostEqual(b, h)

    def test_sidak_grows_with_count(self) -> None:
        a = adjusted_p_value("sidak", 0.01, 1)
        b = adjusted_p_value("sidak", 0.01, 10)
        self.assertGreater(b, a)


class RejectFamilyTests(unittest.TestCase):
    def test_bonferroni_rejects_none_when_all_large(self) -> None:
        policy = MultipleTestingPolicy(method="bonferroni", alpha=0.05)
        p_values = [0.1, 0.2, 0.3, 0.4]
        rejected = reject_family(p_values, policy=policy)
        self.assertFalse(any(rejected))

    def test_bonferroni_rejects_small_enough(self) -> None:
        policy = MultipleTestingPolicy(method="bonferroni", alpha=0.05)
        p_values = [0.001, 0.2, 0.3, 0.4]
        rejected = reject_family(p_values, policy=policy)
        self.assertTrue(rejected[0])
        self.assertFalse(rejected[1])

    def test_holm_step_down(self) -> None:
        policy = MultipleTestingPolicy(method="holm", alpha=0.05)
        p_values = [0.001, 0.01, 0.03, 0.04]
        rejected = reject_family(p_values, policy=policy)
        self.assertTrue(rejected[0])
        self.assertTrue(rejected[1])
        self.assertFalse(rejected[2])

    def test_sidak_rejects_based_on_unadjusted_threshold(self) -> None:
        policy = MultipleTestingPolicy(method="sidak", alpha=0.05)
        p_values = [0.001, 0.02, 0.03, 0.04]
        rejected = reject_family(p_values, policy=policy)
        self.assertTrue(rejected[0])
        self.assertFalse(rejected[1])

    def test_by_controls_fdr(self) -> None:
        policy = MultipleTestingPolicy(method="by", alpha=0.05)
        p_values = [0.001, 0.02, 0.03, 0.04]
        rejected = reject_family(p_values, policy=policy)
        self.assertTrue(rejected[0])

    def test_single_comparison_passes_through(self) -> None:
        policy = MultipleTestingPolicy(method="bonferroni", alpha=0.05)
        rejected = reject_family([0.01], policy=policy)
        self.assertTrue(rejected[0])


class FalsePositiveControlTests(unittest.TestCase):
    """Synthetic false-positive-control tests across many noisy trials.

    When candidates are pure noise (no true effect), the multiple testing
    adjustment should suppress the false-positive rate compared to
    unadjusted testing.
    """

    def test_bonferroni_controls_familywise_error(self) -> None:
        rng = __import__("numpy").random.default_rng(42)
        n_families = 200
        family_size = 20
        alpha = 0.05
        policy = MultipleTestingPolicy(method="bonferroni", alpha=alpha)

        adjusted_positives = 0

        for _ in range(n_families):
            raw_p_values = rng.uniform(0.001, 1.0, size=family_size).tolist()
            rejected = reject_family(raw_p_values, policy=policy)
            adjusted_positives += sum(rejected)

        adjusted_fpr = adjusted_positives / (n_families * family_size)
        self.assertLess(
            adjusted_fpr,
            alpha + 0.05,
            f"adjusted FPR {adjusted_fpr:.4f} exceeds nominal alpha {alpha}",
        )

    def test_holm_controls_familywise_error(self) -> None:
        rng = __import__("numpy").random.default_rng(123)
        n_families = 200
        family_size = 15
        alpha = 0.05
        policy = MultipleTestingPolicy(method="holm", alpha=alpha)

        adjusted_positives = 0
        for _ in range(n_families):
            raw_p_values = rng.uniform(0.001, 1.0, size=family_size).tolist()
            rejected = reject_family(raw_p_values, policy=policy)
            adjusted_positives += sum(rejected)

        adjusted_fpr = adjusted_positives / (n_families * family_size)
        self.assertLess(
            adjusted_fpr,
            alpha + 0.05,
            f"Holm adjusted FPR {adjusted_fpr:.4f} exceeds nominal alpha {alpha}",
        )

    def test_many_noisy_trials_less_likely_to_promote(self) -> None:
        """A single noisy candidate occasionally looks good, but many noisy
        candidates should be suppressed by the adjustment."""
        rng = __import__("numpy").random.default_rng(99)
        baseline = rng.normal(1.0, 0.1, size=48).tolist()
        n_challengers = 50
        alpha = 0.05

        surviving = 0
        for i in range(n_challengers):
            noise = rng.normal(0.0, 0.02, size=48)
            challenger = [v + n for v, n in zip(baseline, noise)]
            alpha_adj = per_comparison_alpha("bonferroni", alpha, n_challengers)
            adjusted_conf = round(1.0 - alpha_adj, 12)
            result = paired_bootstrap_comparison(
                challenger,
                baseline,
                metric="mae_pct",
                lower_is_better=True,
                confidence=adjusted_conf,
                iterations=500,
                seed=i,
                min_samples=32,
            )
            if result["conclusion"] == "candidate_better":
                surviving += 1

        self.assertLess(
            surviving,
            n_challengers * alpha + 5,
            f"Too many survivors ({surviving}/{n_challengers}) under pure noise",
        )


class BootstrapComparisonTests(unittest.TestCase):
    def test_adjusted_comparison_single_candidate(self) -> None:
        candidate = [0.9, 0.9, 0.9, 0.9]
        baseline = [1.0, 1.0, 1.0, 1.0]
        result = adjusted_bootstrap_comparison(
            candidate,
            baseline,
            metric="mae_pct",
            lower_is_better=True,
            policy=MultipleTestingPolicy(method="bonferroni", alpha=0.05),
            comparisons_considered=1,
            rank=1,
            iterations=500,
        )
        self.assertIn("adjusted", result)
        self.assertIn("raw", result)
        self.assertIn("conclusion", result)
        self.assertIn("survives", result)

    def test_adjusted_comparison_larger_family(self) -> None:
        candidate = [0.9, 0.9, 0.9, 0.9]
        baseline = [1.0, 1.0, 1.0, 1.0]
        result_single = adjusted_bootstrap_comparison(
            candidate,
            baseline,
            metric="mae_pct",
            lower_is_better=True,
            policy=MultipleTestingPolicy(method="bonferroni", alpha=0.05),
            comparisons_considered=1,
            rank=1,
            iterations=500,
        )
        result_multi = adjusted_bootstrap_comparison(
            candidate,
            baseline,
            metric="mae_pct",
            lower_is_better=True,
            policy=MultipleTestingPolicy(method="bonferroni", alpha=0.05),
            comparisons_considered=20,
            rank=1,
            iterations=500,
        )
        self.assertGreaterEqual(
            result_single["per_comparison_alpha"],
            result_multi["per_comparison_alpha"],
        )

    def test_family_info_recorded(self) -> None:
        result = adjusted_bootstrap_comparison(
            [0.9] * 4,
            [1.0] * 4,
            metric="mae_pct",
            lower_is_better=True,
            comparisons_considered=5,
            rank=2,
        )
        self.assertEqual(result["family"]["comparisons_considered"], 5)
        self.assertEqual(result["comparison"]["rank"], 2)


class BuildAssessmentTests(unittest.TestCase):
    def test_assessment_basic(self) -> None:
        report = _optimizer_report(n_challengers=3)
        assessment = build_multiple_testing_assessment(report)
        self.assertEqual(assessment["family"]["raw_count"], 3)
        self.assertIn("selected_candidate", assessment)
        self.assertIn("challenger_candidates", assessment)
        self.assertEqual(len(assessment["challenger_candidates"]), 3)

    def test_assessment_with_selected_candidate(self) -> None:
        report = _optimizer_report(n_challengers=5)
        assessment = build_multiple_testing_assessment(report, selected_candidate="variant_2")
        self.assertEqual(assessment["selected_candidate"], "variant_2")

    def test_assessment_unknown_candidate_raises(self) -> None:
        report = _optimizer_report(n_challengers=3)
        with self.assertRaisesRegex(ValueError, "unknown selected candidate"):
            build_multiple_testing_assessment(report, selected_candidate="nonexistent")

    def test_assessment_uses_custom_policy(self) -> None:
        report = _optimizer_report(n_challengers=4)
        policy = MultipleTestingPolicy(method="bonferroni", alpha=0.01)
        assessment = build_multiple_testing_assessment(report, policy=policy)
        self.assertEqual(assessment["policy"]["method"], "bonferroni")
        self.assertAlmostEqual(assessment["policy"]["alpha"], 0.01)

    def test_assessment_no_production_raises(self) -> None:
        report = _optimizer_report()
        report["candidates"] = [c for c in report["candidates"] if c["name"] != "production"]
        with self.assertRaisesRegex(ValueError, "missing the production"):
            build_multiple_testing_assessment(report)

    def test_assessment_no_challengers_raises(self) -> None:
        report = _optimizer_report()
        report["candidates"] = [c for c in report["candidates"] if c["name"] == "production"]
        with self.assertRaisesRegex(ValueError, "no challenger"):
            build_multiple_testing_assessment(report)


class HarmonyNumberTests(unittest.TestCase):
    def test_h1(self) -> None:
        self.assertAlmostEqual(harmonic_number(1), 1.0)

    def test_h2(self) -> None:
        self.assertAlmostEqual(harmonic_number(2), 1.5)

    def test_h3(self) -> None:
        self.assertAlmostEqual(harmonic_number(3), 1.0 + 0.5 + 1.0 / 3.0)


class FamilyRankTests(unittest.TestCase):
    def test_most_significant_rank_one(self) -> None:
        rank = family_rank([0.1, 0.2, 0.3], 0.1)
        self.assertEqual(rank, 1)

    def test_second_most_significant(self) -> None:
        rank = family_rank([0.1, 0.2, 0.3], 0.2)
        self.assertEqual(rank, 2)


if __name__ == "__main__":
    unittest.main()
