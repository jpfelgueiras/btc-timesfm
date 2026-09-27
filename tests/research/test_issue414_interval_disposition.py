"""Integrity checks for the blocked Issue #414 interval disposition."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
RESEARCH = ROOT / "docs" / "research"


class Issue414IntervalDispositionTests(unittest.TestCase):
    def test_disposition_matches_current_issue411_snapshot_and_makes_no_claim(self) -> None:
        source: dict[str, Any] = json.loads(
            (RESEARCH / "ISSUE_411_PARITY_BASELINES_BLOCKED.json").read_text(encoding="utf-8")
        )
        report: dict[str, Any] = json.loads(
            (RESEARCH / "ISSUE_414_INTERVAL_EVIDENCE_BLOCKED.json").read_text(encoding="utf-8")
        )
        snapshot = source["last_verified_prospective_snapshot"]
        evidence = report["evidence"]

        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["disposition"], "no_interval_claim")
        for field in (
            "timestamp_eligible_forecasts",
            "timestamp_eligible_legacy_excluded",
            "confirmatory_forecasts",
            "expected_pairs",
            "matured_pairs",
            "latest_provenance_complete_origin_at",
            "latest_origin_mature_for_cohort",
        ):
            with self.subTest(field=field):
                self.assertEqual(evidence[field], snapshot[field])

        hashes = evidence["hashes_from_issue_411_report_only"]
        self.assertEqual(hashes["code_sha256"], snapshot["code_sha256"])
        self.assertEqual(hashes["database_sha256"], snapshot["database_sha256"])
        self.assertEqual(hashes["last_verified_cohort_content_sha256"], snapshot["cohort_sha256"])
        self.assertIsNone(source["evidence"]["frozen_prospective_cohort"]["sha256"])
        self.assertIsNone(report["source_report"]["snapshot_sha256"])

        interval = report["interval_evaluation"]
        self.assertFalse(interval["scores_computed"])
        self.assertFalse(interval["calibration_fits_computed"])
        self.assertTrue(all(value is None for value in interval["metrics"].values()))
        self.assertIsNone(interval["winner"])
        self.assertFalse(interval["point_forecasts_changed"])

        markdown = (RESEARCH / "ISSUE_414_INTERVAL_EVIDENCE_BLOCKED.md").read_text(encoding="utf-8")
        self.assertIn("Prior-only calibration", markdown)
        self.assertIn("24-origin moving-block bootstrap", markdown)
        self.assertIn("D3", markdown)


if __name__ == "__main__":
    unittest.main()
