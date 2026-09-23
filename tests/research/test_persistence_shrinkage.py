from __future__ import annotations

import unittest

from btc_timesfm.research.persistence_shrinkage import (
    candidate_policy_weights,
    normalize_candidate_weights,
)


class PersistenceShrinkageTests(unittest.TestCase):
    def test_zero_weight_members_remain_zero(self) -> None:
        weights = normalize_candidate_weights({"timesfm_168h": 0.0, "persistence": 4.0})
        self.assertEqual(weights, {"timesfm_168h": 0.0, "persistence": 1.0})

    def test_weight_cap_uses_water_filling(self) -> None:
        weights = normalize_candidate_weights({"a": 10.0, "b": 1.0, "c": 1.0}, max_weight=0.6)
        self.assertAlmostEqual(sum(weights.values()), 1.0)
        self.assertAlmostEqual(weights["a"], 0.6)
        self.assertAlmostEqual(weights["b"], 0.2)
        self.assertAlmostEqual(weights["c"], 0.2)

    def test_candidate_catalog_is_bounded_and_includes_full_persistence(self) -> None:
        names = ["timesfm_168h", "timesfm_336h", "ar1", "persistence"]
        production = {name: 0.25 for name in names}
        policies = candidate_policy_weights(names, production)
        self.assertLessEqual(len(policies), 8)
        self.assertEqual(policies["persistence_only"], {"persistence": 1.0})
        self.assertAlmostEqual(sum(policies["family_equal"].values()), 1.0)
        for weights in policies.values():
            self.assertAlmostEqual(sum(weights.values()), 1.0)

    def test_invalid_weights_and_missing_persistence_fail_closed(self) -> None:
        with self.assertRaises(ValueError):
            normalize_candidate_weights({"persistence": 0.0})
        with self.assertRaises(ValueError):
            normalize_candidate_weights({"persistence": -0.1})
        with self.assertRaises(ValueError):
            candidate_policy_weights(["timesfm_168h"], {"timesfm_168h": 1.0})


if __name__ == "__main__":
    unittest.main()
