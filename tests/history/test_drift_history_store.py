#!/usr/bin/env python3
"""Tests for durable drift inputs and event persistence."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from btc_timesfm.history.history_store import ForecastHistoryStore


class DriftHistoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ForecastHistoryStore(Path(self.tmp.name) / "history.sqlite")
        self.origin = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _snapshot(self) -> dict:
        return {
            "generated_at": self.origin.isoformat(),
            "latest_close_at": self.origin.isoformat(),
            "latest_close_usd": 100.0,
            "source": "Kraken",
            "pair": "BTC/USD",
            "regime": "range",
            "market_features": {
                "volatility_24h_pct": 1.0,
                "range_24h_avg_pct": 1.2,
                "volume_zscore_7d": 0.1,
                "rsi_14": 50.0,
                "momentum_24h_pct": 0.5,
            },
            "model_weights": {"2h": {"persistence": 1.0}},
            "model_predictions": {"persistence": {"2h": {"price_usd": 100.0}}},
            "predictions": {
                "2h": {
                    "price_usd": 101.0,
                    "change_pct": 1.0,
                    "q10_usd": 98.0,
                    "q50_usd": 101.0,
                    "q90_usd": 104.0,
                    "model_agreement": 1.0,
                }
            },
        }

    def test_drift_history_only_returns_matured_prediction_errors(self) -> None:
        self.store.ingest_snapshot(self._snapshot())
        before = self.store.load_drift_history()
        self.assertEqual(before["prediction_rows"], [])
        self.assertEqual(len(before["feature_rows"]), 1)

        target = int((self.origin + timedelta(hours=2)).timestamp())
        self.store.enrich_outcomes({target: 102.0})
        after = self.store.load_drift_history()
        self.assertEqual(len(after["prediction_rows"]), 2)
        self.assertTrue(
            all(row["absolute_error_pct"] is not None for row in after["prediction_rows"])
        )

    def test_drift_events_are_idempotent_and_queryable(self) -> None:
        report = {
            "evaluated_at": "2026-01-02T00:00:00+00:00",
            "latest_observed_origin_at": self.origin.isoformat(),
            "events": [
                {
                    "signal_key": "error:timesfm_168h:2h",
                    "kind": "model_error",
                    "severity": "severe",
                    "model_name": "timesfm_168h",
                    "horizon_hours": 2,
                    "feature_name": None,
                    "metrics": {"ks_distance": 0.8},
                }
            ],
        }
        first = self.store.record_drift_events(report, experiment_run_id="run-1")
        second = self.store.record_drift_events(report, experiment_run_id="run-2")
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)

        events = self.store.recent_drift_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["severity"], "severe")
        self.assertEqual(events[0]["experiment_run_id"], "run-1")
        self.assertEqual(events[0]["metrics"]["ks_distance"], 0.8)
        self.assertEqual(self.store.stats()["drift_events"], 1)
        self.assertEqual(self.store.stats()["latest_drift_severity"], "severe")


if __name__ == "__main__":
    unittest.main()
