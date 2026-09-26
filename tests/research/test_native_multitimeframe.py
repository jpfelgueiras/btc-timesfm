"""Tests for native multi-timeframe UTC aggregation and availability gate."""

from __future__ import annotations

import unittest

from btc_timesfm.research.native_multitimeframe import (
    aggregate_ohlcv_15m,
    build_report,
    exact_target_matches,
    score_exact_targets,
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
        booleans_only = build_report(corpus_available=True, runtime_frequency_supported=True)
        self.assertEqual(booleans_only["status"], "blocked")
        self.assertEqual(booleans_only["matched_targets"], 0)

    def test_scoring_uses_only_exact_targets_with_shared_origins(self) -> None:
        scoring = score_exact_targets(
            {900: 1.0, 1800: 3.0, 2700: 5.0, 3600: 7.0},
            {900: 2.0, 1800: 2.0, 2700: 4.0, 4500: 8.0},
            {900: 0.0, 1800: 1.0, 2700: 3.0, 3600: 6.0, 4500: 7.0},
            hourly_origins={900: 0, 1800: 900, 2700: 1800, 3600: 2700},
            native_origins={900: 0, 1800: 901, 2700: 1800, 4500: 3600},
        )
        self.assertEqual(scoring["matched_targets"], 2)
        self.assertEqual(scoring["eligible_hourly_count"], 2)
        self.assertEqual(scoring["eligible_native_count"], 2)
        self.assertEqual(scoring["hourly_losses"]["mae"], 1.5)
        self.assertEqual(scoring["native_losses"]["mae"], 1.5)
        self.assertAlmostEqual(scoring["residual_correlation"], -1.0)

        report = build_report(
            corpus_available=True,
            runtime_frequency_supported=True,
            corpus_contract={
                "immutable": True,
                "venue": "example",
                "symbol": "BTC/USD",
                "frequency": "15m",
            },
            runtime_contract={"model": "TimesFM", "frequency": "15m", "supported": True},
            scoring=scoring,
        )
        self.assertEqual(report["status"], "ready_for_scoring")
        self.assertEqual(report["matched_targets"], 2)

    def test_missing_origin_or_contract_keeps_report_blocked(self) -> None:
        scoring = score_exact_targets({1: 1.0}, {1: 1.0}, {1: 1.0}, hourly_origins={1: 0})
        self.assertEqual(scoring["matched_targets"], 0)
        without_origins = score_exact_targets({900: 1.0}, {900: 1.0}, {900: 1.0})
        self.assertEqual(without_origins["matched_targets"], 0)
        invalid_horizon = score_exact_targets(
            {1800: 1.0},
            {1800: 1.0},
            {1800: 1.0},
            hourly_origins={1800: 901},
            native_origins={1800: 901},
        )
        self.assertEqual(invalid_horizon["matched_targets"], 0)
        self.assertEqual(
            build_report(
                corpus_available=True,
                runtime_frequency_supported=True,
                scoring={"matched_targets": 10, "eligible_hourly_count": 10,
                         "eligible_native_count": 10, "hourly_losses": {}, "native_losses": {}},
            )["status"],
            "blocked",
        )
        self.assertEqual(
            build_report(
                corpus_available=True,
                runtime_frequency_supported=True,
                corpus_contract={
                    "immutable": True,
                    "venue": "example",
                    "symbol": "BTC/USD",
                    "frequency": "15m",
                },
                runtime_contract={"model": "TimesFM", "frequency": "15m", "supported": True},
                scoring={
                    "origin_validated": True,
                    "matched_targets": 10,
                    "origin_validated_pairs": 0,
                    "eligible_hourly_count": 10,
                    "eligible_native_count": 10,
                    "hourly_losses": {"mae": 1.0, "mse": 1.0},
                    "native_losses": {"mae": 1.0, "mse": 1.0},
                },
            )["status"],
            "blocked",
        )


if __name__ == "__main__":
    unittest.main()
