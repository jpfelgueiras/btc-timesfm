from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from btc_timesfm.history.disaster_recovery import run_drill, write_report
from btc_timesfm.history.history_backup import create_archive
from btc_timesfm.history.history_store import ForecastHistoryStore


class DisasterRecoveryTests(unittest.TestCase):
    def _archive(self, root: Path, origin_at: str) -> Path:
        database = root / "history.sqlite"
        store = ForecastHistoryStore(database)
        store.ingest_snapshot(
            {
                "latest_close_at": origin_at,
                "generated_at": origin_at,
                "source": "test",
                "pair": "BTC/USD",
                "latest_close_usd": 100.0,
                "regime": "range",
                "market_features": {},
                "predictions": {"2h": {"price_usd": 101.0, "change_pct": 1.0}},
            }
        )
        archive = root / "history.sqlite.gz"
        create_archive(database, archive)
        return archive

    def test_drill_reports_restore_counts_and_recent_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = run_drill(
                self._archive(root, "2026-09-16T00:00:00+00:00"),
                now=datetime(2026, 9, 16, tzinfo=timezone.utc),
            )

            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["row_counts"]["forecast_origins"], 1)
            self.assertEqual(report["row_counts"]["forecast_predictions"], 1)
            self.assertTrue(report["recent_content"]["recent"])
            self.assertTrue(report["verification"]["ok"])
            self.assertTrue(report["audit"]["healthy"])

    def test_drill_failure_is_reported_without_changing_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = self._archive(root, "2026-01-01T00:00:00+00:00")
            original = archive.read_bytes()
            report = run_drill(archive, now=datetime(2026, 9, 16, tzinfo=timezone.utc))

            self.assertEqual(report["status"], "failed")
            self.assertIn("no recent forecast origin", report["failure"]["message"])
            self.assertEqual(archive.read_bytes(), original)

    def test_report_writer_persists_failure_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "reports" / "drill.json"
            report = run_drill(Path(directory) / "missing.sqlite.gz")
            write_report(report, output)

            self.assertEqual(json.loads(output.read_text())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
