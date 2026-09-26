"""Tests for the blocked issue #345 evaluation contract."""

import unittest

from btc_timesfm.research.persistence_first_ablation import (
    MAX_POLICY_FAMILIES,
    POLICY_MATRIX,
    build_report,
)
from btc_timesfm.research.persistence_shrinkage import candidate_policy_weights


class PersistenceFirstAblationTests(unittest.TestCase):
    def test_catalog_is_bounded_and_excludes_unverified_alternatives(self) -> None:
        self.assertLessEqual(len(POLICY_MATRIX), MAX_POLICY_FAMILIES)
        self.assertIn("per_context_delete_one", POLICY_MATRIX)
        self.assertNotIn("new_checkpoint", POLICY_MATRIX)
        self.assertNotIn("standalone_ridge", POLICY_MATRIX)

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

    def test_report_enumerates_exact_bounded_catalog_without_generated_candidates(self) -> None:
        report = build_report(
            model_names=["persistence", "timesfm_168"],
            production_weights={"persistence": 0.5, "timesfm_168": 0.5},
        )
        candidates = report["candidate_weights"]
        self.assertEqual([candidate["name"] for candidate in candidates], list(POLICY_MATRIX))
        self.assertEqual(report["policy_matrix"], [candidate["name"] for candidate in candidates])
        self.assertLessEqual(len(candidates), MAX_POLICY_FAMILIES)
        self.assertTrue(
            all(
                candidate
                == {
                    "name": candidate["name"],
                    "status": "planned_not_generated",
                    "weights": None,
                    "eligible": False,
                }
                for candidate in candidates
            )
        )


if __name__ == "__main__":
    unittest.main()
