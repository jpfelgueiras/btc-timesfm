"""Tests for the canonical BTC benchmark data gate."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from btc_timesfm.research.canonical_benchmark import audit_csv


class CanonicalBenchmarkTests(unittest.TestCase):
    def _csv(self, directory: str, pair: str, count: int) -> Path:
        path = Path(directory) / "candles.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("timestamp", "venue", "pair", "open", "high", "low", "close", "volume"))
            for index in range(count):
                writer.writerow((f"2025-01-01T{index:02}:00:00+00:00" if index < 24 else
                                 f"2025-01-{index // 24 + 1:02}T{index % 24:02}:00:00+00:00",
                                 "Example", pair, 100, 101, 99, 100, 1))
        return path

    def test_short_usd_series_is_blocked_and_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = audit_csv(self._csv(directory, "BTC/USD", 48))
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["span_hours"], 48)
        self.assertEqual(len(report["source_sha256"]), 64)
        self.assertFalse(report["eligible_for_skill_comparison"])

    def test_usdt_is_not_accepted_as_usd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = audit_csv(self._csv(directory, "BTCUSDT", 48))
        self.assertEqual(report["status"], "blocked")
        self.assertTrue(any("BTCUSDT is transfer-only" in error for error in report["errors"]))


if __name__ == "__main__":
    unittest.main()
