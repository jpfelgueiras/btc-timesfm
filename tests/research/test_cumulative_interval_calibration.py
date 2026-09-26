from __future__ import annotations

import unittest
from datetime import datetime, timezone

from btc_timesfm.research.cumulative_interval_calibration import (
    build_gate_report,
    interval_metrics,
    matured_residuals,
)


class CumulativeIntervalCalibrationTests(unittest.TestCase):
    def test_residuals_require_raw_lineage_and_target_maturity(self) -> None:
        rows = [
            {
                "stage": "raw_cumulative",
                "raw_lineage_verified": True,
                "horizon": 4,
                "target_at": "2024-01-01T04:00:00+00:00",
                "actual": 12,
                "q50": 10,
            },
            {
                "stage": "raw_cumulative",
                "raw_lineage_verified": True,
                "horizon": 4,
                "target_at": "2024-01-01T05:00:00+00:00",
                "actual": 99,
                "q50": 10,
            },
            {
                "stage": "final",
                "raw_lineage_verified": True,
                "horizon": 4,
                "target_at": "2024-01-01T03:00:00+00:00",
                "actual": 99,
                "q50": 10,
            },
        ]
        self.assertEqual(
            matured_residuals(rows, origin=datetime(2024, 1, 1, 4, tzinfo=timezone.utc), horizon=4),
            [2.0],
        )

    def test_interval_metrics_score_quantiles_and_point_error(self) -> None:
        result = interval_metrics([{"actual": 0, "q10": -1, "q50": 0, "q90": 1}])
        self.assertEqual(result["coverage_80"], 1)
        self.assertEqual(result["mean_width"], 2)
        self.assertEqual(result["mean_absolute_error"], 0)
        self.assertAlmostEqual(result["interval_score_80"], 2)

    def test_missing_lineage_and_data_gates_remain_blocked(self) -> None:
        report = build_gate_report(
            raw_predictions=[],
            matured_outcomes=[],
            dataset_ready=False,
            target_pipeline_ready=False,
        )
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["calibration_skill_claims"])
        self.assertFalse(report["per_step_marginals_as_cumulative_quantiles"])
        self.assertEqual(len(report["blocked_reasons"]), 4)

    def test_naive_timestamps_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            matured_residuals([], origin=datetime(2024, 1, 1), horizon=1)


if __name__ == "__main__":
    unittest.main()
