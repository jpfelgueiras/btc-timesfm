#!/usr/bin/env python3
"""Durable-history integration tests for derivatives feature snapshots."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from history_store import ForecastHistoryStore


class DerivativesHistoryIntegrationTests(unittest.TestCase):
    def test_normalized_raw_and_derived_features_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ForecastHistoryStore(Path(directory) / "history.sqlite")
            origin = datetime(2026, 9, 6, 8, tzinfo=timezone.utc)
            derivative_features = {
                "derivatives_funding_rate_pct": 0.01,
                "derivatives_open_interest_usd": 1_200_000.0,
                "derivatives_oi_change_1h_pct": 2.5,
                "derivatives_oi_change_24h_pct": -3.0,
                "derivatives_long_liquidation_usd_1h": 100.0,
                "derivatives_short_liquidation_usd_1h": 300.0,
                "derivatives_liquidation_total_usd_1h": 400.0,
                "derivatives_liquidation_imbalance": 0.5,
            }
            snapshot = {
                "generated_at": origin.isoformat(),
                "latest_close_at": origin.isoformat(),
                "latest_close_usd": 60_000.0,
                "source": "Kraken hourly OHLC",
                "pair": "BTC/USD",
                "regime": "range",
                "market_features": {"rsi_14": 50.0, **derivative_features},
                "model_weights": {},
                "model_predictions": {},
                "predictions": {
                    "2h": {
                        "price_usd": 60_100.0,
                        "change_pct": 100.0 / 60_000.0 * 100.0,
                        "q10_usd": 59_000.0,
                        "q90_usd": 61_000.0,
                    }
                },
            }
            store.ingest_snapshot(snapshot)
            loaded = store.load_snapshots()[0]
            for name, value in derivative_features.items():
                self.assertEqual(loaded["market_features"][name], value)
            exported = store.export_rows()[0]
            self.assertIn("derivatives_open_interest_usd", exported["market_features_json"])


if __name__ == "__main__":
    unittest.main()
