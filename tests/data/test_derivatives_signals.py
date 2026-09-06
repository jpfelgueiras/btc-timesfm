#!/usr/bin/env python3
"""Tests for timestamp-safe derivatives signal ingestion."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from btc_timesfm.data.derivatives_signals import DERIVATIVE_FEATURE_NAMES, snapshot_from_rows


class DerivativesSignalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.origin = datetime(2026, 9, 6, 8, tzinfo=timezone.utc)
        self.origin_s = int(self.origin.timestamp())

    def test_future_rows_are_ignored_before_feature_derivation(self) -> None:
        funding = [
            {"fundingTime": (self.origin_s - 8 * 3600) * 1000, "fundingRate": "0.0001"},
            {"fundingTime": (self.origin_s + 8 * 3600) * 1000, "fundingRate": "0.99"},
        ]
        stats = [
            {
                "time": self.origin_s - 24 * 3600,
                "open_interest_usd": "1000000",
                "long_liq_usd_new": "100",
                "short_liq_usd_new": "200",
            },
            {
                "time": self.origin_s - 3600,
                "open_interest_usd": "1100000",
                "long_liq_usd_new": "200",
                "short_liq_usd_new": "300",
            },
            {
                "time": self.origin_s,
                "open_interest_usd": "1210000",
                "long_liq_usd_new": "400",
                "short_liq_usd_new": "600",
            },
            {
                "time": self.origin_s + 3600,
                "open_interest_usd": "999999999",
                "long_liq_usd_new": "999999999",
                "short_liq_usd_new": "0",
            },
        ]
        snapshot = snapshot_from_rows(self.origin, funding, stats)
        features = snapshot["features"]
        self.assertEqual(snapshot["status"], "ok")
        self.assertAlmostEqual(features["derivatives_funding_rate_pct"], 0.01)
        self.assertAlmostEqual(features["derivatives_open_interest_usd"], 1210000.0)
        self.assertAlmostEqual(features["derivatives_oi_change_1h_pct"], 10.0)
        self.assertAlmostEqual(features["derivatives_oi_change_24h_pct"], 21.0)
        self.assertEqual(features["derivatives_liquidation_total_usd_1h"], 1000.0)
        self.assertAlmostEqual(features["derivatives_liquidation_imbalance"], 0.2)
        self.assertEqual(set(features), set(DERIVATIVE_FEATURE_NAMES))

    def test_stale_stats_are_omitted_without_aborting_funding(self) -> None:
        funding = [{"fundingTime": (self.origin_s - 2 * 3600) * 1000, "fundingRate": "-0.0002"}]
        stats = [{"time": self.origin_s - 5 * 3600, "open_interest_usd": "1000000"}]
        snapshot = snapshot_from_rows(self.origin, funding, stats)
        self.assertEqual(snapshot["status"], "partial")
        self.assertTrue(snapshot["available"])
        self.assertIn("gate_contract_stats", snapshot["quality"]["stale_sources"])
        self.assertEqual(snapshot["features"], {"derivatives_funding_rate_pct": -0.02})

    def test_all_unavailable_is_explicit_and_non_throwing(self) -> None:
        snapshot = snapshot_from_rows(
            self.origin,
            [],
            [],
            errors={"binance_funding": "Timeout", "gate_contract_stats": "ConnectionError"},
        )
        self.assertEqual(snapshot["status"], "unavailable")
        self.assertFalse(snapshot["available"])
        self.assertEqual(snapshot["features"], {})
        self.assertEqual(
            set(snapshot["quality"]["provider_errors"]),
            {"binance_funding", "gate_contract_stats"},
        )

    def test_missing_24h_history_is_marked_partial(self) -> None:
        funding = [{"fundingTime": self.origin_s * 1000, "fundingRate": "0"}]
        stats = [
            {"time": self.origin_s - 3600, "open_interest_usd": "100"},
            {
                "time": self.origin_s,
                "open_interest_usd": "101",
                "long_liq_usd": "2",
                "short_liq_usd": "3",
            },
        ]
        snapshot = snapshot_from_rows(self.origin, funding, stats)
        self.assertEqual(snapshot["status"], "partial")
        self.assertNotIn("derivatives_oi_change_24h_pct", snapshot["features"])
        self.assertIn("derivatives_oi_change_24h_pct", snapshot["quality"]["missing_features"])


if __name__ == "__main__":
    unittest.main()
