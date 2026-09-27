import hashlib
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = ROOT / "docs/research/ISSUE_416_BLOCKED_FAMILY_CLOSEOUT.json"


class Issue416FamilyCloseoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))

    def test_zero_attempts_cannot_have_a_winner(self) -> None:
        registry = self.registry
        self.assertEqual(registry["status"], "blocked")
        self.assertEqual(registry["summary"]["actual_candidate_attempts"], 0)
        self.assertIsNone(registry["summary"]["winner"])
        self.assertIsNone(registry["summary"]["metrics"])
        for family in registry["families"]:
            with self.subTest(family=family["id"]):
                self.assertEqual(family["status"], "blocked")
                self.assertEqual(family["attempt_status"], "not_attempted")
                self.assertEqual(family["attempt_count"], 0)
                self.assertEqual(family["score_count"], 0)
                self.assertIsNone(family["winner"])
                self.assertIsNone(family["metrics"])

    def test_evidence_hashes_match_and_all_boundaries_are_untouched(self) -> None:
        self.assertEqual(len(self.registry["families"]), 5)
        for family in self.registry["families"]:
            self.assertFalse(family["holdout_accessed"])
            self.assertFalse(family["d3_accessed"])
            self.assertFalse(family["production_mutated"])
            for evidence in family["evidence"]:
                path = ROOT / evidence["path"]
                self.assertTrue(path.is_file(), evidence["path"])
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                self.assertEqual(digest, evidence["sha256"], evidence["path"])

        summary = self.registry["summary"]
        self.assertFalse(summary["holdout_used"])
        self.assertFalse(summary["d3_used"])
        self.assertFalse(summary["production_mutated"])
        self.assertEqual(summary["confirmatory_origins"], 0)
        self.assertEqual(summary["eligible_pairs"], 0)


if __name__ == "__main__":
    unittest.main()
