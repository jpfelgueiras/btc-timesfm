from __future__ import annotations

import copy
import unittest

from btc_timesfm.research.final_selection import validate_contract


def valid_contract() -> dict:
    origins = [
        {
            "origin": _plus_hours(1, i, 0),
            "target": _plus_hours(1, i, horizon),
            "horizon": horizon,
            "raw": {"prediction": 0.0, "interval": [-1, 1]},
            "final": {"prediction": 0.0, "interval": [-1, 1]},
            "failure": None,
            "label_available_at": "2025-02-01T00:00:00+00:00",
        }
        for horizon in (2, 4, 8, 16)
        for i in range(4)
    ]
    prospective_rows = [
        {
            "origin": _plus_hours(1 + i // 24, i % 24, 1),
            "target": _plus_hours(1 + i // 24, i % 24, horizon + 1),
            "horizon": horizon,
            "actual_at": _plus_hours(1 + i // 24, i % 24, horizon + 1),
            "matured_at": _plus_hours(1 + i // 24, i % 24, horizon + 1),
            "actual_value": 100.0,
            "raw": {"prediction": 100.1, "interval": [99.0, 101.0]},
            "final": {"prediction": 100.0, "interval": [99.0, 101.0]},
            "failure": None,
            "source_id": "venue/pair",
            "vintage_id": "v1",
        }
        for horizon in (2, 4, 8, 16)
        for i in range(200)
    ]
    return {
        "freeze": {
            "frozen_at": "2025-02-28T00:00:00+00:00",
            "cutoff_at": "2025-02-27T00:00:00+00:00",
            "evaluation_cutoff": "2025-04-01T00:00:00+00:00",
            "stopping_rule": "fixed 30-day window",
            "corpus_sha256": "abc",
            "family_id": "all-tried-v1",
            "source_id": "venue/pair",
            "vintage_id": "v1",
            "data_id": "dataset-v1",
            "model_id": "model-v1",
            "policy_id": "policy-v1",
            "selection_pairs": [
                {"origin": row["origin"], "target": row["target"], "horizon": row["horizon"]}
                for row in origins
            ],
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
                "acceptance": _acceptance(),
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
                "acceptance": _acceptance(),
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
            "evaluation_cutoff": "2025-04-01T00:00:00+00:00",
            "pairs": {
                "challenger-a": prospective_rows,
                "challenger-b": copy.deepcopy(prospective_rows),
            },
            "fixed_cutoff": True,
            "stopping_rule": "fixed 30-day window",
            "final_holdout_disjoint": True,
        },
    }


def _plus_hours(day: int, hour: int, delta: int) -> str:
    from datetime import datetime, timedelta, timezone

    value = datetime(2025, 3, day, hour, tzinfo=timezone.utc) + timedelta(hours=delta)
    return value.isoformat()


def _acceptance() -> dict:
    return {
        "preregistered": True,
        "adjusted_improvement_pct": 3.1,
        "adjusted_ci_lower_pct": 0.1,
        "positive_skill_vs": {"persistence": 0.03, "strongest_naive": 0.02},
        "powered_regression_pct": [4.9, 2.0],
        "directional_loss_pp": 1.9,
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
        contract["prospective"]["pairs"]["challenger-a"] = [
            row
            for row in contract["prospective"]["pairs"]["challenger-a"]
            if not (row["horizon"] == 8 and row["origin"] == "2025-03-01T01:00:00+00:00")
        ]
        report = validate_contract(contract)
        self.assertTrue(any("30 calendar days" in item for item in report["reasons"]))
        self.assertTrue(
            any("fewer than 200 exact mature pairs at 8h" in item for item in report["reasons"])
        )

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

    def test_non_integer_horizon_and_count_values_block_without_crashing(self) -> None:
        contract = valid_contract()
        contract["candidates"][0]["pairs"][0]["horizon"] = 2.9
        contract["selection"]["candidate_family_count"] = "3"
        contract["prospective"]["pairs"]["challenger-a"][0]["horizon"] = "2"
        report = validate_contract(contract)
        self.assertEqual(report["status"], "blocked")
        self.assertTrue(any("exact positive integer" in item for item in report["reasons"]))
        self.assertTrue(any("family count" in item for item in report["reasons"]))

    def test_frozen_selection_rows_require_exact_utc_and_target_arithmetic(self) -> None:
        contract = valid_contract()
        contract["freeze"]["selection_pairs"][0]["origin"] = "2025-03-01T01:00:00+01:00"
        contract["freeze"]["selection_pairs"][1]["target"] = "not-a-timestamp"
        contract["freeze"]["selection_pairs"][2]["target"] = "2025-03-01T13:00:00+00:00"
        report = validate_contract(contract)
        self.assertTrue(any("must use UTC" in item for item in report["reasons"]))
        self.assertTrue(any("ISO-8601" in item for item in report["reasons"]))
        self.assertTrue(
            any("target must equal origin plus horizon" in item for item in report["reasons"])
        )

    def test_candidate_selection_rows_are_validated_not_only_compared(self) -> None:
        contract = valid_contract()
        contract["candidates"][0]["pairs"][0]["target"] = "2025-03-01T13:00:00+00:00"
        contract["candidates"][0]["pairs"][1]["origin"] = "2025-03-01T01:00:00-01:00"
        report = validate_contract(contract)
        self.assertTrue(
            any("target must equal origin plus horizon" in item for item in report["reasons"])
        )
        self.assertTrue(any("must use UTC" in item for item in report["reasons"]))

    def test_attempt_family_includes_non_mapping_entries(self) -> None:
        contract = valid_contract()
        contract["candidates"].append(None)
        report = validate_contract(contract)
        self.assertEqual(report["attempted_candidate_count"], 4)
        self.assertTrue(any("candidate[3] is not a manifest" in item for item in report["reasons"]))
        self.assertTrue(any("family count" in item for item in report["reasons"]))

    def test_prospective_requires_recorded_values_stages_and_identity(self) -> None:
        contract = valid_contract()
        row = contract["prospective"]["pairs"]["challenger-a"][0]
        del row["actual_value"]
        del row["raw"]
        row["failure"] = "inference failed"
        row["vintage_id"] = "wrong-vintage"
        report = validate_contract(contract)
        self.assertTrue(any("actual_value" in item for item in report["reasons"]))
        self.assertTrue(any("raw/final/failure tracking" in item for item in report["reasons"]))
        self.assertTrue(
            any("failed prospective forecast rows" in item for item in report["reasons"])
        )
        self.assertTrue(any("identity mismatch: vintage_id" in item for item in report["reasons"]))

    def test_stage_presence_booleans_are_not_forecast_evidence(self) -> None:
        contract = valid_contract()
        selection_row = contract["candidates"][0]["pairs"][0]
        selection_row["raw"] = True
        prospective_row = contract["prospective"]["pairs"]["challenger-a"][0]
        prospective_row["raw"] = True
        report = validate_contract(contract)
        self.assertTrue(
            any("dropped raw/final forecast tracking" in item for item in report["reasons"])
        )
        self.assertTrue(
            any(
                "prospective row missing raw/final/failure tracking" in item
                for item in report["reasons"]
            )
        )

    def test_evaluation_cutoff_must_match_frozen_cutoff_and_follow_maturity(self) -> None:
        contract = valid_contract()
        contract["prospective"]["evaluation_cutoff"] = "2025-03-30T00:00:00+00:00"
        report = validate_contract(contract)
        self.assertTrue(
            any("exactly match the frozen manifest cutoff" in item for item in report["reasons"])
        )

    def test_frozen_selection_pair_set_is_required_and_exact(self) -> None:
        contract = valid_contract()
        del contract["freeze"]["selection_pairs"]
        report = validate_contract(contract)
        self.assertTrue(
            any("nonempty frozen selection_pairs" in item for item in report["reasons"])
        )

    def test_prospective_actual_target_maturity_and_start_are_proven(self) -> None:
        contract = valid_contract()
        contract["prospective"]["start_at"] = contract["freeze"]["frozen_at"]
        row = contract["prospective"]["pairs"]["challenger-a"][0]
        row["origin"] = contract["prospective"]["start_at"]
        row["target"] = "2025-03-02T00:00:00+00:00"
        row["actual_at"] = "2025-03-03T00:00:00+00:00"
        row["matured_at"] = "2025-04-02T00:00:00+00:00"
        report = validate_contract(contract)
        self.assertTrue(any("start after the freeze" in item for item in report["reasons"]))
        self.assertTrue(
            any("actual_at must exactly equal target" in item for item in report["reasons"])
        )
        self.assertTrue(
            any("target must equal origin plus horizon" in item for item in report["reasons"])
        )
        self.assertTrue(any("origin is not after D3 start" in item for item in report["reasons"]))
        self.assertTrue(any("after evaluation cutoff" in item for item in report["reasons"]))

    def test_prospective_counts_are_derived_from_records(self) -> None:
        contract = valid_contract()
        contract["prospective"]["pairs"]["challenger-a"] = contract["prospective"]["pairs"][
            "challenger-a"
        ][:-1]
        report = validate_contract(contract)
        self.assertTrue(
            any("fewer than 200 exact mature pairs at 16h" in item for item in report["reasons"])
        )

    def test_acceptance_thresholds_fail_closed(self) -> None:
        contract = valid_contract()
        evidence = contract["candidates"][0]["acceptance"]
        evidence["adjusted_improvement_pct"] = 2.99
        evidence["adjusted_ci_lower_pct"] = 0
        evidence["positive_skill_vs"]["persistence"] = 0
        evidence["powered_regression_pct"] = [5.01]
        evidence["directional_loss_pp"] = 2.01
        report = validate_contract(contract)
        self.assertTrue(any("at least 3%" in item for item in report["reasons"]))
        self.assertTrue(any("lower bound must exceed 0" in item for item in report["reasons"]))
        self.assertTrue(any("positive skill" in item for item in report["reasons"]))
        self.assertTrue(any("exceeds 5%" in item for item in report["reasons"]))
        self.assertTrue(any("2 percentage points" in item for item in report["reasons"]))


if __name__ == "__main__":
    unittest.main()
