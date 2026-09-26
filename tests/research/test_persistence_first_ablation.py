"""Tests for the blocked issue #345 evaluation contract."""

import unittest

from btc_timesfm.research.persistence_first_ablation import (
    MAX_POLICY_FAMILIES,
    PLANNED_POLICY_FAMILIES,
    build_report,
)
from btc_timesfm.research.persistence_shrinkage import candidate_policy_weights


class PersistenceFirstAblationTests(unittest.TestCase):
    def test_catalog_is_bounded_and_excludes_unverified_alternatives(self) -> None:
        self.assertLessEqual(len(PLANNED_POLICY_FAMILIES), MAX_POLICY_FAMILIES)
        self.assertIn("per_context_delete_one", PLANNED_POLICY_FAMILIES)
        self.assertNotIn("new_checkpoint", PLANNED_POLICY_FAMILIES)
        self.assertNotIn("standalone_ridge", PLANNED_POLICY_FAMILIES)

    def test_candidate_mechanics_allow_full_persistence(self) -> None:
        names = ["persistence", "timesfm_168", "timesfm_336", "drift_7d", "ar1"]
        candidates = candidate_policy_weights(names, {name: 0.2 for name in names})
        self.assertEqual(candidates["persistence_only"], {"persistence": 1.0})
        self.assertAlmostEqual(sum(candidates["persistence_only"].values()), 1.0)

    def test_empty_corpus_report_is_blocked_without_forecasts(self) -> None:
        report = build_report()
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["decision"], "no_candidate_passes")
        self.assertEqual(report["corpus"]["eligible_origin_count"], 0)
        self.assertFalse(report["inference_performed"])
        self.assertFalse(report["forecasts_fabricated"])
        self.assertFalse(report["evidence"]["d2"]["acceptance_eligible"])
        self.assertEqual(report["additional_ablations"]["ridge"]["status"], "blocked")

    def test_report_separates_planned_families_from_generated_weight_vectors(self) -> None:
        report = build_report(
            model_names=["persistence", "timesfm_168"],
            production_weights={"persistence": 0.5, "timesfm_168": 0.5},
        )
        candidates = report["candidate_weights"]
        expected = candidate_policy_weights(
            ["persistence", "timesfm_168"],
            {"persistence": 0.5, "timesfm_168": 0.5},
        )
        self.assertEqual([candidate["name"] for candidate in candidates], list(expected))
        self.assertEqual([candidate["weights"] for candidate in candidates], list(expected.values()))
        self.assertTrue(all(candidate["eligible"] is False for candidate in candidates))
        self.assertTrue(
            all(candidate["status"] == "weights_generated_not_evaluated" for candidate in candidates)
        )
        self.assertEqual(report["planned_policy_families"], list(PLANNED_POLICY_FAMILIES))
        self.assertTrue(set(report["planned_policy_families"]).isdisjoint(expected))
        self.assertIn("per_context_delete_one", report["planned_policy_families"])

    def test_report_without_inputs_has_no_candidate_vectors(self) -> None:
        report = build_report()
        self.assertEqual(report["candidate_weights"], [])
        self.assertTrue(all(not candidate["eligible"] for candidate in report["candidate_weights"]))


if __name__ == "__main__":
    unittest.main()
