from __future__ import annotations

import unittest
import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from btc_timesfm.research.candidate_confirmation import (
    PROTOCOL,
    _canonical_json,
    _valid_parity_report,
    build_report,
    main,
    protocol_sha256,
    validate_outcome,
)


def completed_outcome(status: str) -> dict:
    disposition = {
        "inconclusive": "criteria_unresolved",
        "negative": "criteria_failed",
        "ready_for_human_review": "criteria_passed",
    }[status]
    result = {
        "status": status,
        "candidate_winner": None,
        "candidates_scored": True,
        "candidates_preregistered": True,
        "protocol_sha256": protocol_sha256(),
        "evaluation_report_sha256": "b" * 64,
        "attempts_complete": True,
        "attempts": [
            {
                "attempt_id": "attempt-a",
                "candidate_id": "candidate-a",
                "disposition": "scored",
                "failure": None,
            }
        ],
        "selection_disposition": disposition,
    }
    manifest = {
        "protocol_sha256": protocol_sha256(),
        "candidates": [{"candidate_id": "candidate-a"}],
        "planned_attempts": [{"attempt_id": "attempt-a", "candidate_id": "candidate-a"}],
    }
    result["preregistration_manifest"] = manifest
    result["preregistration_sha256"] = hashlib.sha256(_canonical_json(manifest)).hexdigest()
    if status == "ready_for_human_review":
        result.update(
            proposed_candidate_id="candidate-a",
            frozen_criteria_passed=True,
            human_review_only=True,
            automatic_promotion=False,
        )
    return result


class CandidateConfirmationTests(unittest.TestCase):
    def test_unavailable_parity_is_blocked_without_scoring_or_winner(self) -> None:
        report = build_report({"status": "blocked"}, parity_path="input.json", parity_sha256="abc")
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(
            [item["code"] for item in report["blockers"]],
            ["parity_diagnosis_unavailable", "candidate_family_not_preregistered"],
        )
        self.assertFalse(report["candidates_preregistered"])
        self.assertFalse(report["candidates_scored"])
        self.assertEqual(report["attempts"], [])
        self.assertIsNone(report["candidate_winner"])
        self.assertFalse(report["skill_inference"])
        self.assertEqual(
            report["evidence"]["parity_report"], {"path": "input.json", "sha256": "abc"}
        )
        self.assertEqual(report["protocol_sha256"], protocol_sha256())
        self.assertEqual(
            protocol_sha256(),
            hashlib.sha256(
                json.dumps(
                    PROTOCOL, sort_keys=True, separators=(",", ":"), ensure_ascii=True
                ).encode()
            ).hexdigest(),
        )

    def test_statuses_remain_distinct_and_forged_winners_fail(self) -> None:
        blocked = validate_outcome(
            {
                "status": "blocked",
                "blockers": [{"code": "no_data"}],
                "candidates_scored": False,
                "candidates_preregistered": False,
                "attempts": [],
                "candidate_winner": None,
            }
        )
        self.assertTrue(blocked["valid"], blocked)
        for status in ("inconclusive", "negative", "ready_for_human_review"):
            result = validate_outcome(completed_outcome(status))
            self.assertTrue(result["valid"], result)
            self.assertEqual(result["status"], status)
        forged = completed_outcome("ready_for_human_review")
        forged["candidate_winner"] = "unvalidated"
        forged = validate_outcome(forged)
        self.assertFalse(forged["valid"])
        self.assertIsNone(forged["candidate_winner"])

    def test_human_review_requires_frozen_criteria_and_disables_promotion(self) -> None:
        for field, value in (("frozen_criteria_passed", False), ("automatic_promotion", True)):
            forged = completed_outcome("ready_for_human_review")
            forged[field] = value
            self.assertFalse(validate_outcome(forged)["valid"])

    def test_completed_outcomes_require_scored_attempt_and_complete_reconciliation(self) -> None:
        for status in ("inconclusive", "negative", "ready_for_human_review"):
            only_failures = completed_outcome(status)
            only_failures["attempts"][0].update(disposition="failed", failure="runtime error")
            self.assertFalse(validate_outcome(only_failures)["valid"])

            missing = completed_outcome(status)
            missing["attempts"] = []
            self.assertFalse(validate_outcome(missing)["valid"])

            mismatched = completed_outcome(status)
            mismatched["attempts"][0]["candidate_id"] = "candidate-forged"
            self.assertFalse(validate_outcome(mismatched)["valid"])

            wrong_hash = completed_outcome(status)
            wrong_hash["preregistration_sha256"] = "c" * 64
            self.assertFalse(validate_outcome(wrong_hash)["valid"])

    def test_manifest_protocol_hash_and_evaluation_digest_are_required(self) -> None:
        valid = completed_outcome("inconclusive")
        self.assertTrue(validate_outcome(valid)["valid"], validate_outcome(valid))
        wrong_protocol = completed_outcome("negative")
        wrong_protocol["preregistration_manifest"]["protocol_sha256"] = "d" * 64
        wrong_protocol["preregistration_sha256"] = hashlib.sha256(
            _canonical_json(wrong_protocol["preregistration_manifest"])
        ).hexdigest()
        self.assertFalse(validate_outcome(wrong_protocol)["valid"])
        missing_evaluation_digest = completed_outcome("negative")
        del missing_evaluation_digest["evaluation_report_sha256"]
        self.assertFalse(validate_outcome(missing_evaluation_digest)["valid"])

    def test_attempt_ids_and_candidate_ids_must_be_unique_in_plan_and_observations(self) -> None:
        duplicate_observed = completed_outcome("negative")
        duplicate_observed["attempts"].append(dict(duplicate_observed["attempts"][0]))
        self.assertFalse(validate_outcome(duplicate_observed)["valid"])

        duplicate_planned = completed_outcome("negative")
        duplicate_planned["preregistration_manifest"]["planned_attempts"].append(
            {"attempt_id": "attempt-b", "candidate_id": "candidate-a"}
        )
        duplicate_planned["preregistration_sha256"] = hashlib.sha256(
            _canonical_json(duplicate_planned["preregistration_manifest"])
        ).hexdigest()
        self.assertFalse(validate_outcome(duplicate_planned)["valid"])

    def test_malformed_status_and_disposition_fail_closed(self) -> None:
        self.assertFalse(validate_outcome({"status": []})["valid"])
        malformed = completed_outcome("negative")
        malformed["attempts"][0]["disposition"] = []
        self.assertFalse(validate_outcome(malformed)["valid"])

    def test_proposed_candidate_must_be_registered_and_scored(self) -> None:
        unregistered = completed_outcome("ready_for_human_review")
        unregistered["proposed_candidate_id"] = "other"
        self.assertFalse(validate_outcome(unregistered)["valid"])
        unscored = completed_outcome("ready_for_human_review")
        unscored["attempts"][0].update(disposition="failed", failure="failure")
        self.assertFalse(validate_outcome(unscored)["valid"])

    def test_unready_status_and_forged_blocked_fields_fail(self) -> None:
        self.assertFalse(validate_outcome({"status": "ready", "candidate_winner": None})["valid"])
        result = validate_outcome(
            {
                "status": "blocked",
                "candidate_winner": None,
                "candidates_scored": True,
                "candidates_preregistered": True,
                "attempts": [{"candidate_id": "x"}],
            }
        )
        self.assertFalse(result["valid"])

    def test_status_only_completed_outcomes_are_rejected(self) -> None:
        for status in ("inconclusive", "negative"):
            self.assertFalse(
                validate_outcome({"status": status, "candidate_winner": None})["valid"]
            )

    def test_parity_requires_all_real_schema_gates_and_consistent_null_claim_fields(self) -> None:
        valid = {
            "schema_version": 1,
            "issue": 399,
            "status": "ready_for_replay",
            "gates": {"corpus": "passed", "eligible_mature_production_policy_evidence": "passed"},
            "blockers": [],
            "canonical_skill_claim": False,
            "metrics": None,
        }
        self.assertTrue(_valid_parity_report(valid))
        variants = [
            {**valid, "schema_version": 2},
            {**valid, "issue": 400},
            {**valid, "blockers": [{"code": "contradiction"}]},
            {**valid, "canonical_skill_claim": True},
            {**valid, "metrics": {}},
            {**valid, "gates": {**valid["gates"], "corpus": "blocked"}},
            {"status": "ready_for_replay"},
        ]
        for forged in variants:
            self.assertFalse(_valid_parity_report(forged), forged)

    def test_ready_parity_still_blocks_until_candidate_family_is_preregistered(self) -> None:
        ready_parity = {
            "schema_version": 1,
            "issue": 399,
            "status": "ready_for_replay",
            "gates": {
                "corpus": "passed",
                "eligible_mature_production_policy_evidence": "passed",
            },
            "blockers": [],
            "canonical_skill_claim": False,
            "metrics": None,
        }
        report = build_report(ready_parity)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["blockers"][0]["code"], "candidate_family_not_preregistered")
        self.assertFalse(report["candidates_preregistered"])
        self.assertFalse(report["candidates_scored"])
        self.assertIsNone(report["candidate_winner"])

    def test_cli_hashes_exact_parity_bytes_and_malformed_utf8_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parity_path = Path(directory) / "parity.json"
            output_path = Path(directory) / "outcome.json"
            content = b'{"status":"ready_for_replay"}\n'
            parity_path.write_bytes(content)
            with patch(
                "sys.argv",
                [
                    "candidate_confirmation",
                    "--parity-report",
                    str(parity_path),
                    "--output",
                    str(output_path),
                ],
            ):
                main()
            report = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(
                report["evidence"]["parity_report"]["sha256"], hashlib.sha256(content).hexdigest()
            )
            parity_path.write_bytes(b"\xff")
            with patch(
                "sys.argv",
                [
                    "candidate_confirmation",
                    "--parity-report",
                    str(parity_path),
                    "--output",
                    str(output_path),
                ],
            ):
                main()
            report = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(
                report["evidence"]["parity_report"]["sha256"],
                hashlib.sha256(b"\xff").hexdigest(),
            )


if __name__ == "__main__":
    unittest.main()
