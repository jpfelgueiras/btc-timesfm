"""Tests for the canonical BTC benchmark data gate."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from btc_timesfm.research.canonical_benchmark import (
    HOUR_SECONDS,
    TARGET_END_TS,
    TARGET_START_TS,
    WARMUP_START_TS,
    audit_csv,
)


FIELDS = (
    "timestamp",
    "venue",
    "pair",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "vintage",
    "revision",
)


def _iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


class CanonicalBenchmarkTests(unittest.TestCase):
    def _write_rows(self, directory: str, timestamps: list[int], *, mutate=None) -> Path:
        path = Path(directory) / "candles.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            for index, timestamp in enumerate(timestamps):
                row = {
                    "timestamp": _iso(timestamp),
                    "venue": "Kraken",
                    "pair": "XBT/USD",
                    "open": "100",
                    "high": "101",
                    "low": "99",
                    "close": "100",
                    "volume": "1",
                    "vintage": _iso(timestamp),
                    "revision": "0",
                }
                if mutate:
                    mutate(row, index)
                writer.writerow(row)
        return path

    @staticmethod
    def _complete_timestamps() -> list[int]:
        return list(range(WARMUP_START_TS, TARGET_END_TS, HOUR_SECONDS))

    def test_complete_target_period_and_warmup_pass_coverage_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = audit_csv(self._write_rows(directory, self._complete_timestamps()))
        expected_target = (TARGET_END_TS - TARGET_START_TS) // HOUR_SECONDS
        self.assertEqual(report["status"], "ready_for_replay")
        self.assertEqual(report["target_period"]["observed_hours"], expected_target)
        self.assertEqual(report["target_period"]["missing_hours"], 0)
        self.assertEqual(report["warmup_period"]["observed_hours"], 180 * 24)
        self.assertEqual(report["warmup_period"]["missing_hours"], 0)
        self.assertFalse(report["eligible_for_skill_comparison"])

    def test_vintage_at_or_before_close_is_accepted(self) -> None:
        timestamps = self._complete_timestamps()

        def prior_vintage(row, index):
            if index == 0:
                row["vintage"] = _iso(timestamps[0] - HOUR_SECONDS)

        with tempfile.TemporaryDirectory() as directory:
            report = audit_csv(self._write_rows(directory, timestamps, mutate=prior_vintage))
        self.assertEqual(report["status"], "ready_for_replay")

    def test_target_and_warmup_are_independently_required(self) -> None:
        timestamps = self._complete_timestamps()
        timestamps.remove(TARGET_START_TS)
        with tempfile.TemporaryDirectory() as directory:
            target_report = audit_csv(self._write_rows(directory, timestamps))
        self.assertEqual(target_report["status"], "blocked")
        self.assertEqual(target_report["target_period"]["missing_hours"], 1)

        timestamps = self._complete_timestamps()
        timestamps.remove(WARMUP_START_TS)
        with tempfile.TemporaryDirectory() as directory:
            warmup_report = audit_csv(self._write_rows(directory, timestamps))
        self.assertEqual(warmup_report["status"], "blocked")
        self.assertEqual(warmup_report["warmup_period"]["missing_hours"], 1)

    def test_mixed_venue_or_pair_is_blocked(self) -> None:
        timestamps = self._complete_timestamps()

        def mutate(row, index):
            if index == 0:
                row["venue"] = "Bitstamp"
                row["pair"] = "BTC/USD"

        with tempfile.TemporaryDirectory() as directory:
            report = audit_csv(self._write_rows(directory, timestamps, mutate=mutate))
        self.assertEqual(report["status"], "blocked")
        self.assertIsNone(report["venue"])
        self.assertIsNone(report["pair"])

    def test_revisions_duplicates_and_future_vintage_are_blocked(self) -> None:
        timestamps = self._complete_timestamps()

        def revised(row, index):
            if index == 0:
                row["revision"] = "1"

        with tempfile.TemporaryDirectory() as directory:
            report = audit_csv(self._write_rows(directory, timestamps, mutate=revised))
        self.assertEqual(report["status"], "blocked")
        self.assertTrue(any("revision must be exactly 0" in error for error in report["errors"]))

        with tempfile.TemporaryDirectory() as directory:
            report = audit_csv(self._write_rows(directory, timestamps + [timestamps[0]]))
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["duplicates"], 1)

        def future_vintage(row, index):
            if index == 0:
                row["vintage"] = _iso(timestamps[0] + 2 * HOUR_SECONDS)

        with tempfile.TemporaryDirectory() as directory:
            report = audit_csv(self._write_rows(directory, timestamps, mutate=future_vintage))
        self.assertEqual(report["status"], "blocked")
        self.assertTrue(any("not point-in-time eligible" in error for error in report["errors"]))

        def one_hour_late(row, index):
            if index == 0:
                row["vintage"] = _iso(timestamps[0] + HOUR_SECONDS)

        with tempfile.TemporaryDirectory() as directory:
            report = audit_csv(self._write_rows(directory, timestamps, mutate=one_hour_late))
        self.assertEqual(report["status"], "blocked")
        self.assertTrue(any("not point-in-time eligible" in error for error in report["errors"]))

    def test_gaps_and_invalid_ohlcv_are_blocked(self) -> None:
        timestamps = self._complete_timestamps()
        timestamps.remove(TARGET_START_TS + 10 * HOUR_SECONDS)
        with tempfile.TemporaryDirectory() as directory:
            report = audit_csv(self._write_rows(directory, timestamps))
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["gaps"], 1)

        def invalid(row, index):
            if index == 0:
                row["low"] = "102"

        with tempfile.TemporaryDirectory() as directory:
            report = audit_csv(self._write_rows(directory, self._complete_timestamps(), mutate=invalid))
        self.assertEqual(report["status"], "blocked")
        self.assertGreater(report["invalid_rows"], 0)

    def test_cli_without_data_writes_blocked_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "audit.json"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "btc_timesfm.research.canonical_benchmark",
                    "--output",
                    str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["target_period_observations"], 0)
        self.assertFalse(report["eligible_for_skill_comparison"])
        self.assertEqual(json.loads(result.stdout)["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
