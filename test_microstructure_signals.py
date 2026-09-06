from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from microstructure_signals import (
    MICROSTRUCTURE_FEATURE_NAMES,
    fetch_microstructure_snapshot,
    snapshot_from_book,
)


class MicrostructureSignalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.origin = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
        self.captured = self.origin + timedelta(minutes=30)
        self.bids = [["9999", "2"], ["9995", "4"], ["9980", "10"]]
        self.asks = [["10001", "1"], ["10005", "3"], ["10020", "10"]]

    def test_snapshot_derives_spread_depth_imbalance_and_microprice(self) -> None:
        snapshot = snapshot_from_book(
            self.origin,
            self.captured,
            self.bids,
            self.asks,
            provider="test",
            pair="BTC/USD",
        )
        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(set(snapshot["features"]), set(MICROSTRUCTURE_FEATURE_NAMES))
        self.assertGreater(snapshot["features"]["microstructure_spread_bps"], 0)
        self.assertGreater(snapshot["features"]["microstructure_bid_depth_usd_25bps"], 0)
        self.assertGreater(snapshot["features"]["microstructure_ask_depth_usd_25bps"], 0)
        self.assertGreater(snapshot["features"]["microstructure_imbalance_10bps"], 0)

    def test_future_or_late_capture_is_rejected(self) -> None:
        too_late = snapshot_from_book(
            self.origin,
            self.origin + timedelta(hours=2),
            self.bids,
            self.asks,
            provider="test",
            pair="BTC/USD",
        )
        self.assertEqual(too_late["status"], "unavailable")
        self.assertIn("capture_too_late", too_late["quality"]["errors"])
        self.assertFalse(too_late["features"])

    def test_crossed_or_empty_book_is_unavailable(self) -> None:
        crossed = snapshot_from_book(
            self.origin,
            self.captured,
            [["10002", "1"]],
            [["10001", "1"]],
            provider="test",
            pair="BTC/USD",
        )
        self.assertEqual(crossed["status"], "unavailable")
        self.assertIn("crossed_book", crossed["quality"]["errors"])

    @patch("microstructure_signals.requests.get")
    def test_provider_failure_falls_back_to_bitstamp(self, get: Mock) -> None:
        kraken = Mock()
        kraken.raise_for_status.side_effect = Exception("boom")
        bitstamp = Mock()
        bitstamp.raise_for_status.return_value = None
        bitstamp.json.return_value = {"bids": self.bids, "asks": self.asks}
        get.side_effect = [kraken, bitstamp]

        # The implementation catches requests exceptions. Use a request-style error.
        import requests

        kraken.raise_for_status.side_effect = requests.RequestException("boom")
        snapshot = fetch_microstructure_snapshot(self.origin, captured_at=self.captured)
        self.assertEqual(snapshot["provider"]["name"], "bitstamp_spot")
        self.assertTrue(snapshot["available"])
        self.assertIn("kraken:RequestException", snapshot["quality"]["errors"])

    @patch("microstructure_signals.requests.get")
    def test_both_providers_down_degrades_without_raising(self, get: Mock) -> None:
        import requests

        response = Mock()
        response.raise_for_status.side_effect = requests.RequestException("down")
        get.return_value = response
        snapshot = fetch_microstructure_snapshot(self.origin, captured_at=self.captured)
        self.assertEqual(snapshot["status"], "unavailable")
        self.assertFalse(snapshot["available"])
        self.assertEqual(snapshot["provider"]["name"], "none")


if __name__ == "__main__":
    unittest.main()
