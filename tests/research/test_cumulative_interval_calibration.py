from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

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

    def test_interval_metrics_use_standard_wis_with_nonzero_median_error(self) -> None:
        result = interval_metrics([{"actual": 2, "q10": 0, "q50": 1, "q90": 3}])
        self.assertAlmostEqual(result["pinball_q10"], 0.2)
        self.assertAlmostEqual(result["pinball_q50"], 0.5)
        self.assertAlmostEqual(result["pinball_q90"], 0.1)
        self.assertEqual(result["interval_score_80"], 3)
        self.assertAlmostEqual(result["mean_absolute_error"], 1)
        self.assertAlmostEqual(result["weighted_interval_score"], (0.5 * 1 + 0.1 * 3) / 1.5)
        self.assertAlmostEqual(result["weighted_interval_score"], (0.2 + 0.5 + 0.1) / 1.5)

    @staticmethod
    def _valid_inputs() -> tuple[list[dict], list[dict], dict]:
        predictions = []
        outcomes = []
        frozen = datetime(2024, 2, 1, tzinfo=timezone.utc)
        prospective_start = datetime(2024, 3, 1, tzinfo=timezone.utc)
        evaluated = datetime(2024, 3, 31, tzinfo=timezone.utc)
        for index in range(400):
            origin = (
                datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(hours=index * 3)
                if index < 200
                else prospective_start + timedelta(hours=(index - 200) * 3)
            )
            target = origin + timedelta(hours=4)
            for stage in ("raw_cumulative", "pre_coherence", "final"):
                predictions.append(
                    {
                        "stage": stage,
                        "origin_at": origin.isoformat(),
                        "target_at": target.isoformat(),
                        "horizon": 4,
                        "source_identity": "source-v1",
                        "candidate_identity": "candidate-v1",
                    }
                )
            outcomes.append(
                {
                    "origin_at": origin.isoformat(),
                    "target_at": target.isoformat(),
                    "horizon": 4,
                    "actual": 1.2,
                    "period": "prospective" if index < 200 else "evaluation",
                    "actual_at": target.isoformat(),
                    "matured_at": (target + timedelta(minutes=1)).isoformat(),
                }
            )
        audit = {
            "status": "ready",
            "manifest_sha256": "a" * 64,
            "exact_target_coverage": True,
            "prospective_period_ready": True,
            "required_horizons": [4],
            "frozen_at": frozen.isoformat(),
            "prospective_start": prospective_start.isoformat(),
            "evaluated_at": evaluated.isoformat(),
        }
        return predictions, outcomes, audit

    def _gate(self, predictions: list[dict], outcomes: list[dict], audit: dict) -> dict:
        return build_gate_report(
            raw_predictions=predictions,
            matured_outcomes=outcomes,
            canonical_audit=audit,
            target_pipeline_ready=True,
        )

    def test_complete_linked_lineage_and_exact_pair_can_pass_small_fixture_gate(self) -> None:
        predictions, outcomes, audit = self._valid_inputs()
        report = self._gate(predictions, outcomes, audit)
        self.assertEqual(report["status"], "ready_for_evaluation")
        self.assertEqual(report["paired_counts_by_horizon"], {4: 200})
        self.assertEqual(report["prospective_paired_counts_by_horizon"], {4: 200})

    def test_one_row_partial_duplicate_mismatched_and_unpaired_data_block(self) -> None:
        predictions, outcomes, audit = self._valid_inputs()
        cases = [
            (predictions[:1], outcomes),
            (predictions[:2], outcomes),
            (predictions + [predictions[0]], outcomes),
            (
                [
                    {**row, "candidate_identity": "other"} if row["stage"] == "final" else row
                    for row in predictions
                ],
                outcomes,
            ),
            (predictions, [{**outcomes[0], "target_at": "2024-01-01T04:01:00+00:00"}]),
        ]
        for prediction_rows, outcome_rows in cases:
            with self.subTest(predictions=len(prediction_rows), outcomes=outcome_rows):
                self.assertEqual(
                    self._gate(prediction_rows, outcome_rows, audit)["status"], "blocked"
                )

    def test_bare_ready_flags_and_insufficient_prospective_support_block(self) -> None:
        predictions, outcomes, _ = self._valid_inputs()
        report = build_gate_report(
            raw_predictions=predictions,
            matured_outcomes=outcomes,
            target_pipeline_ready=True,
        )
        self.assertEqual(report["status"], "blocked")
        for outcome in outcomes:
            outcome["period"] = "evaluation"
        self.assertEqual(
            self._gate(
                predictions,
                outcomes,
                {
                    "status": "ready",
                    "manifest_sha256": "a" * 64,
                    "exact_target_coverage": True,
                    "prospective_period_ready": True,
                    "required_horizons": [4],
                },
            )["status"],
            "blocked",
        )

    def test_period_labels_cannot_bypass_temporal_evidence(self) -> None:
        predictions, outcomes, audit = self._valid_inputs()
        for outcome in outcomes:
            outcome["period"] = "prospective"
        report = self._gate(predictions, outcomes, audit)
        self.assertEqual(report["status"], "ready_for_evaluation")
        future = [{**row, "matured_at": "2024-04-01T00:00:00+00:00"} for row in outcomes]
        self.assertEqual(self._gate(predictions, future, audit)["status"], "blocked")
        false_actual = [{**row, "actual_at": row["target_at"]} for row in outcomes]
        false_actual[0]["actual_at"] = "2024-01-01T00:00:00+00:00"
        self.assertEqual(self._gate(predictions, false_actual, audit)["status"], "blocked")
        too_early = {**audit, "evaluated_at": "2024-03-30T00:00:00+00:00"}
        self.assertEqual(self._gate(predictions, outcomes, too_early)["status"], "blocked")
        naive = {**audit, "frozen_at": "2024-02-01T00:00:00"}
        self.assertEqual(self._gate(predictions, outcomes, naive)["status"], "blocked")

    def test_naive_timestamps_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            matured_residuals([], origin=datetime(2024, 1, 1), horizon=1)


if __name__ == "__main__":
    unittest.main()
