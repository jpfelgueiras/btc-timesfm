import json
import unittest
from pathlib import Path


ARTIFACT = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "research"
    / "ISSUE_409_PIT_CORPUS_COMPARISON.json"
)


class Issue409CorpusComparisonTests(unittest.TestCase):
    def test_comparison_is_machine_readable_and_blocked_without_relaxed_contract(self) -> None:
        report = json.loads(ARTIFACT.read_text(encoding="utf-8"))

        self.assertEqual(report["status"], "blocked")
        self.assertIsNone(report["admitted_source"])
        self.assertGreaterEqual(len(report["comparison"]), 3)
        self.assertEqual(report["contract"]["target_hours"], 32136)
        self.assertEqual(report["contract"]["warmup_hours"], 4320)
        self.assertIn("BTCUSDT or any non-fiat-USD quote pair", report["prohibited_substitutions"])
        self.assertIn("mixed venues", report["prohibited_substitutions"])
        self.assertTrue(report["procurement"]["not_obtained"])
        self.assertTrue(report["reproducible_next_action"])
        procurement = report["procurement"]
        self.assertIn("project owner/budget authority", procurement["responsible_role"].casefold())
        self.assertIn("Before any provider inquiry", procurement["authorization_gate"])
        inquiry = procurement["first_inquiry"]
        self.assertEqual(inquiry["provider"], "Kaiko")
        self.assertEqual(inquiry["submission_status"], "not_sent")
        self.assertEqual(inquiry["response_status"], "not_requested")
        self.assertIn("contact-kaiko", inquiry["url"])
        self.assertIn(
            "Kraken XBT/USD",
            procurement["inquiry_specification"]["text_to_send_after_authorization"],
        )
        self.assertIn(
            "keep the source inadmissible",
            procurement["inquiry_specification"]["fallback_if_unavailable"],
        )
        for candidate in report["comparison"]:
            with self.subTest(channel=candidate["channel"]):
                self.assertTrue(candidate["source_urls"])
                self.assertRegex(candidate["decision"], r"^(Not admitted|Rejected)")


if __name__ == "__main__":
    unittest.main()
