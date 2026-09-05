#!/usr/bin/env python3
"""Unit tests for the lightweight GitHub Actions schedule guard."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from schedule_guard import (
    completed_hour,
    latest_forecast_close,
    parse_timestamp,
    should_run,
)


class ScheduleGuardTests(unittest.TestCase):
    def test_parse_timestamp_accepts_z_and_naive_values(self) -> None:
        self.assertEqual(
            parse_timestamp("2026-09-05T12:00:00Z"),
            datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            parse_timestamp("2026-09-05T12:00:00"),
            datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc),
        )

    def test_parse_timestamp_rejects_invalid_values(self) -> None:
        for value in (None, "", 123, "not-a-date"):
            self.assertIsNone(parse_timestamp(value))

    def test_completed_hour_normalizes_to_utc_and_floors(self) -> None:
        west = timezone(timedelta(hours=1))
        value = datetime(2026, 9, 5, 15, 47, 31, tzinfo=west)
        self.assertEqual(
            completed_hour(value),
            datetime(2026, 9, 5, 14, 0, tzinfo=timezone.utc),
        )

    def test_latest_forecast_close_returns_newest_valid_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 3,
                        "forecasts": [
                            {"latest_close_at": "2026-09-05T08:00:00+00:00"},
                            {"latest_close_at": "bad"},
                            {"latest_close_at": "2026-09-05T10:00:00Z"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                latest_forecast_close(path),
                datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc),
            )

    def test_latest_forecast_close_supports_legacy_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(
                json.dumps(
                    {
                        "latest_close_at": "2026-09-05T09:00:00+00:00",
                        "predictions": {"2h": {}},
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                latest_forecast_close(path),
                datetime(2026, 9, 5, 9, 0, tzinfo=timezone.utc),
            )

    def test_manual_run_always_proceeds(self) -> None:
        run, reason = should_run("workflow_dispatch", Path("missing-state.json"))
        self.assertTrue(run)
        self.assertIn("Manual", reason)

    def test_scheduled_run_without_history_proceeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run, reason = should_run("schedule", Path(tmp) / "missing.json")
            self.assertTrue(run)
            self.assertIn("No usable", reason)

    def test_scheduled_run_waits_for_two_completed_candles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(
                json.dumps(
                    {
                        "forecasts": [
                            {"latest_close_at": "2026-09-05T09:00:00+00:00"}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            run, reason = should_run(
                "schedule",
                path,
                now=datetime(2026, 9, 5, 10, 59, tzinfo=timezone.utc),
            )
            self.assertFalse(run)
            self.assertIn("1.0 completed", reason)

            run, reason = should_run(
                "schedule",
                path,
                now=datetime(2026, 9, 5, 11, 1, tzinfo=timezone.utc),
            )
            self.assertTrue(run)
            self.assertIn("2.0 completed", reason)


if __name__ == "__main__":
    unittest.main()
