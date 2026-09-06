#!/usr/bin/env python3
"""Unit tests for forecast scoring and rolling history state."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np

from tests.support.unit_test_stubs import install_timesfm_stub

install_timesfm_stub()

from btc_timesfm.cli import btc_forecast  # noqa: E402


class ForecastScoringTests(unittest.TestCase):
    def test_score_price_reports_error_direction_and_interval(self) -> None:
        score = btc_forecast.score_price(100.0, 110.0, 105.0, 103.0, 111.0)
        self.assertEqual(score["predicted_price_usd"], 110.0)
        self.assertEqual(score["actual_price_usd"], 105.0)
        self.assertAlmostEqual(score["absolute_error_pct"], 4.7619)
        self.assertGreater(score["signed_error_pct"], 0.0)
        self.assertTrue(score["direction_correct"])
        self.assertTrue(score["within_q10_q90"])

    def test_score_price_detects_wrong_direction(self) -> None:
        score = btc_forecast.score_price(100.0, 95.0, 105.0)
        self.assertFalse(score["direction_correct"])
        self.assertIsNone(score["within_q10_q90"])

    def test_score_snapshot_requires_exact_matured_target(self) -> None:
        origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
        snapshot = {
            "latest_close_at": origin.isoformat(),
            "latest_close_usd": 100.0,
            "regime": "range",
            "predictions": {"2h": {"price_usd": 102.0}},
            "model_predictions": {},
        }
        self.assertIsNone(
            btc_forecast.score_snapshot(
                snapshot,
                2,
                {int((origin + timedelta(hours=2, seconds=1)).timestamp()): 101.0},
            )
        )
        score = btc_forecast.score_snapshot(
            snapshot,
            2,
            {int((origin + timedelta(hours=2)).timestamp()): 101.0},
        )
        self.assertIsNotNone(score)
        assert score is not None
        self.assertEqual(score["actual_price_usd"], 101.0)

    def test_score_forecast_history_uses_most_recent_matured_snapshot(self) -> None:
        origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
        older = {
            "latest_close_at": origin.isoformat(),
            "latest_close_usd": 100.0,
            "predictions": {"2h": {"price_usd": 102.0}},
            "model_predictions": {},
        }
        newer = {
            "latest_close_at": (origin + timedelta(hours=1)).isoformat(),
            "latest_close_usd": 101.0,
            "predictions": {"2h": {"price_usd": 103.0}},
            "model_predictions": {},
        }
        timestamps = [
            int((origin + timedelta(hours=2)).timestamp()),
            int((origin + timedelta(hours=3)).timestamp()),
        ]
        closes = np.asarray([101.5, 102.5])
        reliability = btc_forecast.score_forecast_history([older, newer], closes, timestamps)
        self.assertEqual(reliability["2h"]["forecast_origin_at"], newer["latest_close_at"])
        self.assertEqual(reliability["2h"]["actual_price_usd"], 102.5)

    def test_performance_summary_aggregates_matured_samples(self) -> None:
        origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
        history = []
        timestamps = []
        closes = []
        for offset, predicted, actual in ((0, 102.0, 101.0), (1, 104.0, 103.0)):
            current_origin = origin + timedelta(hours=offset)
            history.append(
                {
                    "latest_close_at": current_origin.isoformat(),
                    "latest_close_usd": 100.0 + offset,
                    "predictions": {"2h": {"price_usd": predicted}},
                    "model_predictions": {"persistence": {"2h": {"price_usd": 100.0 + offset}}},
                }
            )
            timestamps.append(int((current_origin + timedelta(hours=2)).timestamp()))
            closes.append(actual)
        summary = btc_forecast.performance_summary(history, np.asarray(closes), timestamps)
        self.assertEqual(summary["2h"]["samples"], 2)
        self.assertEqual(summary["2h"]["models"]["persistence"]["samples"], 2)
        self.assertGreater(summary["2h"]["mae_pct"], 0.0)


class ForecastStateTests(unittest.TestCase):
    def test_load_history_supports_versioned_and_legacy_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            snapshots = [{"latest_close_at": "2026-01-01T00:00:00+00:00"}]
            with patch.object(btc_forecast, "STATE_PATH", path):
                path.write_text(json.dumps({"version": 2, "forecasts": snapshots}))
                self.assertEqual(btc_forecast.load_forecast_history(), snapshots)
                legacy = {"latest_close_at": "legacy", "predictions": {}}
                path.write_text(json.dumps(legacy))
                self.assertEqual(btc_forecast.load_forecast_history(), [legacy])

    def test_load_history_ignores_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not-json")
            with patch.object(btc_forecast, "STATE_PATH", path):
                self.assertEqual(btc_forecast.load_forecast_history(), [])

    def test_save_history_deduplicates_origin_and_respects_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            old = {
                "latest_close_at": "2026-01-01T00:00:00+00:00",
                "predictions": {"2h": {}},
            }
            output = {
                "generated_at": "2026-01-01T01:00:01+00:00",
                "latest_close_at": "2026-01-01T00:00:00+00:00",
                "latest_close_usd": 100.0,
                "regime": "range",
                "market_features": {},
                "model_weights": {},
                "model_predictions": {},
                "predictions": {"2h": {"price_usd": 101.0}},
            }
            with (
                patch.object(btc_forecast, "STATE_PATH", path),
                patch.object(btc_forecast, "HISTORY_LIMIT", 2),
            ):
                btc_forecast.save_forecast_history([old, {"latest_close_at": "older"}], output)

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["version"], 4)
            self.assertEqual(len(saved["forecasts"]), 2)
            origins = [item["latest_close_at"] for item in saved["forecasts"]]
            self.assertEqual(origins.count(output["latest_close_at"]), 1)
            self.assertEqual(saved["forecasts"][-1]["latest_close_usd"], 100.0)


if __name__ == "__main__":
    unittest.main()
