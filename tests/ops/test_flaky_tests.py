"""Tests for flaky-test retry, logging and quarantine policy."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from btc_timesfm.ops.flaky_tests import (
    failed_test_ids,
    load_log,
    load_quarantines,
    record_outcomes,
    retry_command,
    save_log,
    verdict,
)


class FlakyTestPolicyTests(unittest.TestCase):
    def test_extracts_unique_failed_test_ids(self) -> None:
        output = """FAIL: test_alpha (tests.ops.test_policy.PolicyTests.test_alpha)
ERROR: test_beta (tests.ops.test_policy.PolicyTests.test_beta)
FAIL: test_alpha (tests.ops.test_policy.PolicyTests.test_alpha)
"""
        self.assertEqual(
            failed_test_ids(output),
            [
                "tests.ops.test_policy.PolicyTests.test_alpha",
                "tests.ops.test_policy.PolicyTests.test_beta",
            ],
        )

    def test_retry_command_runs_only_failed_tests_without_coverage(self) -> None:
        command = ["unittest-parallel", "-s", "tests", "--coverage"]
        self.assertEqual(
            retry_command(command, ["tests.ops.test_policy.PolicyTests.test_alpha"]),
            ["unittest-parallel", "-t", ".", "tests.ops.test_policy.PolicyTests.test_alpha"],
        )

    def test_records_passed_and_failed_reruns_across_runs(self) -> None:
        log = record_outcomes({}, ["test_alpha", "test_beta"], ["test_beta"])
        log = record_outcomes(log, ["test_alpha"], [])
        self.assertEqual(
            log,
            {
                "test_alpha": {"failures": 2, "rerun_passes": 2, "rerun_failures": 0},
                "test_beta": {"failures": 1, "rerun_passes": 0, "rerun_failures": 1},
            },
        )

    def test_log_round_trip_persists_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "flake-log.json"
            log = record_outcomes({}, ["test_alpha"], [])
            save_log(path, log)
            self.assertEqual(load_log(path), log)
            self.assertEqual(json.loads(path.read_text())["schema_version"], 1)

    def test_open_issue_quarantine_excludes_only_rerun_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quarantines.json"
            path.write_text(json.dumps({"tests": {"test_alpha": {"issue": 42}}}))
            quarantines = load_quarantines(path, {42})
            blocked, quarantined = verdict(
                ["test_alpha", "test_beta"], ["test_alpha", "test_beta"], quarantines
            )
            self.assertEqual(blocked, ["test_beta"])
            self.assertEqual(quarantined, ["test_alpha"])

    def test_closed_or_missing_issue_is_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quarantines.json"
            path.write_text(json.dumps({"tests": {"test_alpha": {"issue": 42}}}))
            quarantines = load_quarantines(path, set())
            blocked, quarantined = verdict(["test_alpha"], ["test_alpha"], quarantines)
            self.assertEqual(blocked, ["test_alpha"])
            self.assertEqual(quarantined, [])

    def test_quarantine_requires_positive_tracking_issue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quarantines.json"
            path.write_text(json.dumps({"tests": {"test_alpha": {"issue": 0}}}))
            with self.assertRaisesRegex(ValueError, "positive issue"):
                load_quarantines(path, {0})


if __name__ == "__main__":
    unittest.main()
