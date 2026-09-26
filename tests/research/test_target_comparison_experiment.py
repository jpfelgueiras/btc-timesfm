from __future__ import annotations

import math
import unittest

import numpy as np

from btc_timesfm.research.target_comparison_experiment import (
    accumulate_return_path,
    build_report,
    cumulative_quantile_paths,
)


class TargetComparisonExperimentTests(unittest.TestCase):
    def test_hourly_path_is_causally_accumulated_from_origin(self) -> None:
        path = accumulate_return_path(100.0, [math.log(1.1), math.log(2.0)])
        np.testing.assert_allclose(path, [110.0, 220.0])

    def test_quantiles_are_accumulated_and_order_checked(self) -> None:
        paths = cumulative_quantile_paths(
            100.0,
            {
                "q10": [0.0, 0.0],
                "q50": [0.01, 0.0],
                "q90": [0.02, 0.0],
            },
        )
        self.assertLessEqual(paths["q10"][1], paths["q50"][1])
        self.assertLessEqual(paths["q50"][1], paths["q90"][1])
        with self.assertRaisesRegex(ValueError, "cross"):
            cumulative_quantile_paths(
                100.0,
                {"q10": [0.1], "q50": [0.0], "q90": [0.2]},
            )

    def test_no_corpus_or_supported_head_is_blocked(self) -> None:
        report = build_report([])
        self.assertEqual(report["decision"]["decision"], "blocked")
        self.assertFalse(report["decision"]["promotion_eligible"])
        self.assertIn("not_candidate_evidence", report["evidence"]["kind"])
        self.assertIn("joint predictive distribution", report["quantile_semantics"])


if __name__ == "__main__":
    unittest.main()
