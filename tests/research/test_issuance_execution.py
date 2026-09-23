from __future__ import annotations

import unittest

from btc_timesfm.research.issuance_execution import (
    executable_entry,
    select_non_overlapping_positions,
)


class IssuanceExecutionTests(unittest.TestCase):
    def test_entry_uses_first_price_not_before_or_unknown_at_issue(self) -> None:
        entry = executable_entry(
            "2026-01-01T10:20:00Z",
            "2026-01-01T12:00:00Z",
            {
                "2026-01-01T10:00:00Z": 100.0,
                "2026-01-01T10:30:00Z": 101.0,
                "2026-01-01T11:00:00Z": 102.0,
            },
        )
        self.assertEqual(entry, {"entry_at": "2026-01-01T10:30:00+00:00", "entry_price_usd": 101.0})

    def test_missing_entry_price_or_late_generation_returns_none(self) -> None:
        self.assertIsNone(
            executable_entry(
                "2026-01-01T10:20:00Z",
                "2026-01-01T12:00:00Z",
                {"2026-01-01T10:00:00Z": 100.0},
            )
        )
        self.assertIsNone(executable_entry("2026-01-01T12:00:00Z", "2026-01-01T12:00:00Z", {}))

    def test_overlap_rule_is_deterministic_and_reports_skips(self) -> None:
        result = select_non_overlapping_positions(
            [
                {
                    "id": "later",
                    "entry_at": "2026-01-01T11:00:00Z",
                    "target_at": "2026-01-01T12:00:00Z",
                },
                {
                    "id": "first",
                    "entry_at": "2026-01-01T10:30:00Z",
                    "target_at": "2026-01-01T12:30:00Z",
                },
            ]
        )
        self.assertEqual([item["id"] for item in result["retained"]], ["first"])
        self.assertEqual(result["skipped"][0]["id"], "later")
        self.assertEqual(result["skipped"][0]["skip_reason"], "position_already_open")

    def test_timestamps_must_be_timezone_aware(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone"):
            executable_entry("2026-01-01T10:20:00", "2026-01-01T12:00:00Z", {})


if __name__ == "__main__":
    unittest.main()
