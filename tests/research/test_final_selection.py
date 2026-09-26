from __future__ import annotations

import copy
import unittest

from btc_timesfm.research.final_selection import validate_contract


def valid_contract() -> dict:
    origins = [
        {
            "origin": f"2025-01-01T{i:02d}:00:00+00:00",
            "target": f"2025-01-01T{i + horizon:02d}:00:00+00:00",
            "horizon": horizon,
            "raw": {"prediction": 0.0, "interval": [-1, 1]},
            "final": {"prediction": 0.0, "interval": [-1, 1]},
            "failure": None,
            "label_available_at": "2025-02-01T00:00:00+00:00",
        }
        for horizon in (2, 4, 8, 16)
        for i in range(4)
    ]
    return {
        "freeze": {
            "frozen_at": "2025-03-01T00:00:00+00:00",
            "cutoff_at": "2025-02-28T00:00:00+00:00",
            "stopping_rule": "fixed 30-day window",
            "corpus_sha256": "abc",
            "family_id": "all-tried-v1",
            "source_id": "venue/pair",
            "vintage_id": "v1",
            "data_id": "dataset-v1",
            "model_id": "model-v1",
            "policy_id": "policy-v1",
        },
        "candidates": [
            {
                "candidate_id": "challenger-a",
                "status": "eligible",
                "source_id": "venue/pair",
                "vintage_id": "v1",
                "data_id": "dataset-v1",
                "model_id": "model-v1",
                "policy_id": "policy-v1",
                "pairs": origins,
            },
            {
                "candidate_id": "challenger-b",
                "status": "eligible",
                "source_id": "venue/pair",
                "vintage_id": "v1",
                "data_id": "dataset-v1",
                "model_id": "model-v1",
                "policy_id": "policy-v1",
                "pairs": copy.deepcopy(origins),
            },
            {"candidate_id": "failed-attempt", "status": "failed"},
        ],
        "selection": {
            "candidate_family_count": 3,
            "adjustment": "holm",
            "dependence_method": "moving_block",
            "baselines": ["champion", "persistence", "strongest_naive"],
            "nested_folds": True,
            "final_holdout_reused": False,
        },
        "prospective": {
            "start_at": "2025-03-01T00:00:00+00:00",
            "end_at": "2025-04-01T00:00:00+00:00",
            "exact_mature_pairs_by_horizon": {"2": 200, "4": 200, "8": 200, "16": 200},
            "fixed_cutoff": True,
            "stopping_rule": "fixed 30-day window",
            "final_holdout_disjoint": True,
        },
    }


class FinalSelectionTests(unittest.TestCase):
    def test_missing_repository_inputs_are_blocked(self) -> None:
        report = validate_contract({})
        self.assertEqual(report["status"], "blocked")
        self.assertGreater(len(report["reasons"]), 1)

    def test_valid_synthetic_contract_is_ready_but_selects_no_winner(self) -> None:
        report = validate_contract(valid_contract())
        self.assertEqual(report["status"], "ready_for_evaluation", report["reasons"])
        self.assertIsNone(report["candidate_winner"])

    def test_invalid_candidate_status_and_family_accounting_block(self) -> None:
        contract = valid_contract()
        contract["candidates"][1]["status"] = "skipped"
        report = validate_contract(contract)
        self.assertTrue(any("invalid status" in item for item in report["reasons"]))

    def test_mismatched_origins_block(self) -> None:
        contract = valid_contract()
        contract["candidates"][1]["pairs"].pop()
        report = validate_contract(contract)
        self.assertTrue(any("exact paired origin/target set" in item for item in report["reasons"]))

    def test_dropped_raw_or_final_or_untracked_failure_blocks(self) -> None:
        contract = valid_contract()
        contract["candidates"][0]["pairs"][0]["final"] = None
        report = validate_contract(contract)
        self.assertTrue(any("dropped raw/final" in item for item in report["reasons"]))

    def test_incomplete_horizons_block(self) -> None:
        contract = valid_contract()
        contract["candidates"][0]["pairs"] = [
            row for row in contract["candidates"][0]["pairs"] if row["horizon"] != 16
        ]
        report = validate_contract(contract)
        self.assertTrue(any("all four horizons" in item for item in report["reasons"]))

    def test_prospective_duration_and_pair_floor_block(self) -> None:
        contract = valid_contract()
        contract["prospective"]["end_at"] = "2025-03-20T00:00:00+00:00"
        contract["prospective"]["exact_mature_pairs_by_horizon"]["8"] = 199
        report = validate_contract(contract)
        self.assertTrue(any("30 calendar days" in item for item in report["reasons"]))
        self.assertTrue(any("200 exact mature pairs at 8h" in item for item in report["reasons"]))

    def test_holdout_overlap_blocks(self) -> None:
        contract = valid_contract()
        contract["selection"]["final_holdout_reused"] = True
        contract["prospective"]["final_holdout_disjoint"] = False
        report = validate_contract(contract)
        self.assertTrue(any("final holdout" in item for item in report["reasons"]))

    def test_selection_labels_after_cutoff_block(self) -> None:
        contract = valid_contract()
        contract["candidates"][0]["pairs"][0]["label_available_at"] = "2025-03-01T00:00:00+00:00"
        report = validate_contract(contract)
        self.assertTrue(any("after cutoff" in item for item in report["reasons"]))

    def test_source_identity_mismatch_blocks(self) -> None:
        contract = copy.deepcopy(valid_contract())
        contract["candidates"][0]["vintage_id"] = "other-vintage"
        report = validate_contract(contract)
        self.assertTrue(any("identity mismatch" in item for item in report["reasons"]))


if __name__ == "__main__":
    unittest.main()
