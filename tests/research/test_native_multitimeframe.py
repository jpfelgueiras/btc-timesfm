"""Tests for native multi-timeframe UTC aggregation and availability gate."""

from __future__ import annotations

import unittest

from btc_timesfm.research.native_multitimeframe import (
    aggregate_ohlcv_15m,
    build_report,
    exact_target_matches,
)


def candles(timestamps: list[int]) -> dict[str, list[float] | list[int]]:
    return {
        "timestamps": timestamps,
        "opens": [10.0 + i for i in range(len(timestamps))],
        "highs": [12.0 + i for i in range(len(timestamps))],
        "lows": [9.0 + i for i in range(len(timestamps))],
        "closes": [11.0 + i for i in range(len(timestamps))],
        "volumes": [1.0] * len(timestamps),
    }


class NativeMultiTimeframeTests(unittest.TestCase):
    def test_ohlcv_aggregation_and_utc_day_boundary(self) -> None:
        # Three bars closing at midnight, then three opening at midnight UTC.
        result = aggregate_ohlcv_15m(candles([85_500, 85_800, 86_100, 86_400, 86_700, 87_000]))
        self.assertEqual([row["timestamp"] for row in result], [86_400, 87_300])
        self.assertEqual(
            {key: result[0][key] for key in ("open", "high", "low", "close", "volume")},
            {"open": 10.0, "high": 14.0, "low": 9.0, "close": 13.0, "volume": 3.0},
        )

    def test_partial_and_gap_bars_are_excluded(self) -> None:
        self.assertEqual(aggregate_ohlcv_15m(candles([0, 300])), [])
        self.assertEqual(aggregate_ohlcv_15m(candles([0, 600, 900])), [])

    def test_rejects_misaligned_or_out_of_order_utc_input(self) -> None:
        with self.assertRaisesRegex(ValueError, "UTC boundaries"):
            aggregate_ohlcv_15m(candles([1]))
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            aggregate_ohlcv_15m(candles([300, 0]))

    def test_targets_match_exactly_and_summary_control_is_separate(self) -> None:
        matches = exact_target_matches({900: 1.0, 1800: 2.0}, {900: 3.0, 2700: 4.0})
        self.assertEqual(matches, [{"target_ts": 900, "hourly": 1.0, "native": 3.0}])
        report = build_report()
        self.assertEqual(report["status"], "blocked")
        self.assertIsNone(report["accuracy_claim"])
        self.assertIn("separate control", report["summary_ridge_control"])
        self.assertIn("corpus", report["blockers"][0])
        self.assertIn("frequency", report["blockers"][1])
        ready = build_report(corpus_available=True, runtime_frequency_supported=True)
        self.assertEqual(ready["status"], "ready_for_scoring")
        self.assertIsNone(ready["accuracy_claim"])


if __name__ == "__main__":
    unittest.main()
