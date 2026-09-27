import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = ROOT / "docs/research/ISSUE_417_D3_BLOCKED_NO_CANDIDATE.json"


class Issue417BlockedD3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))

    def test_blocked_no_candidate_outcome_is_consistent_with_source_evidence(self) -> None:
        report = self.report
        self.assertEqual(report["status"], "blocked")
        self.assertIsNone(report["candidate"])
        self.assertIsNone(report["champion"])
        self.assertIsNone(report["candidate_champion_pair"])
        self.assertFalse(report["d3"]["launched"])
        self.assertEqual(report["d3"]["origins"], 0)
        self.assertEqual(report["d3"]["pairs"], 0)
        self.assertEqual(set(report["d3"]["pairs_by_horizon"].values()), {0})
        self.assertIsNone(report["d3"]["metrics"])
        self.assertEqual(report["d3"]["accuracy_peeks"], 0)
        self.assertFalse(report["d3"]["outcomes_accessed"])
        self.assertFalse(report["production_mutated"])

        for evidence in report["evidence"]:
            with self.subTest(issue=evidence["issue"]):
                path = ROOT / evidence["path"]
                self.assertTrue(path.is_file(), evidence["path"])
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), evidence["sha256"])

        issue411 = json.loads(
            (ROOT / "docs/research/ISSUE_411_PARITY_BASELINES_BLOCKED.json").read_text(
                encoding="utf-8"
            )
        )
        issue416 = json.loads(
            (ROOT / "docs/research/ISSUE_416_BLOCKED_FAMILY_CLOSEOUT.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(issue411["last_verified_prospective_snapshot"]["matured_pairs"], 0)
        self.assertEqual(issue416["summary"]["actual_candidate_attempts"], 0)
        self.assertIsNone(issue416["summary"]["winner"])
        self.assertFalse(issue416["summary"]["d3_used"])

    def test_reopening_contract_covers_issue_417_requirements(self) -> None:
        contract = self.report["reopening_contract"]["pre_frozen_before_any_d3_score"]
        self.assertEqual(len(contract), 7)
        text = " ".join(contract).lower()
        for requirement in (
            "model/checkpoint",
            "code",
            "policy",
            "source/vintage",
            "disjoint",
            "30 calendar days",
            "200 exact mature pairs",
            "no repeated accuracy",
            "family-adjusted",
            "human review",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, text)


if __name__ == "__main__":
    unittest.main()
