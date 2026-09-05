#!/usr/bin/env python3
"""Unit tests for forecast scoring, history loading, and state persistence."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np

from unit_test_stubs import install_timesfm_stub

install_timesfm_stub()

import btc_forecast  # noqa: E402


class ForecastScoringTests(unittest.TestCase):
    def test_score_price_reports_error_direction_and_interval(self) -> None:
        score = btc_forecast.score_price(100.0, 102.0, 101.0, 99.0, 103.0)
        self.assertEqual(score["absolute_error_usd"], 1.0)
        self.assertAlmostEqual(score["absolute_error_pct"], 0.9901, places=4)
        self.assertGreater(score["signed_error_pct"], 0)
        self.assertTrue(score["direction_correct"])
        self.assertTrue(score["within_q10_q90"])

    def test_score_price_detects_wrong_direction(self) -> None:
        score = btc_forecast.score_price(100.0, 99.0, 102.0)
        self.assertFalse(score["direction_correct"])
        self.assertIsNone(score["within_q10_q90"])

    def test_score_snapshot_requires_exact_matured_target(self) -> None:
        origin = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        snapshot = {
            "latest_close_at": origin.isoformat(),
            "latest_close_usd": 100.0,
            "regime": "range",
            "predictions": {"2h": {"price_usd": 101.0, "q10_usd": 98.0, "q90_usd": 104.0}},
            "model_predictions": {
                "persistence": {"2h": {"price_usd": 100.0}},
                "ar1": {"2h": {"price_usd": 101.5}},
            },
        }
        target = int((origin + timedelta(hours=2)).timestamp())
        self.assertIsNone(btc_forecast.score_snapshot(snapshot, 2, {}))

        score = btc_forecast.score_snapshot(snapshot, 2, {target: 102.0})
        self.assertIsNotNone(score)
        assert score is not None
        self.assertEqual(score["horizon"], "2h")
        self.assertEqual(score["regime"], "range")
        self.assertEqual(set(score["models"]), {"persistence", "ar1"})
        self.assertTrue(score["within_q10_q90"])

    def test_score_forecast_history_uses_most_recent_matured_snapshot(self) -> None:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        older = {
            "latest_close_at": base.isoformat(),
            "latest_close_usd": 100.0,
            "predictions": {"2h": {"price_usd": 101.0}},
        }
        newer_origin = base + timedelta(hours=2)
        newer = {
            "latest_close_at": newer_origin.isoformat(),
            "latest_close_usd": 102.0,
            "predictions": {"2h": {"price_usd": 103.0}},
        }
        timestamps = [
            int((base + timedelta(hours=2)).timestamp()),
            int((newer_origin + timedelta(hours=2)).timestamp()),
        ]
        closes = np.asarray([101.5, 104.0], dtype=np.float32)
        reliability = btc_forecast.score_forecast_history([older, newer], closes, timestamps)
        self.assertEqual(reliability["2h"]["forecast_origin_at"], newer_origin.isoformat())
        self.assertEqual(reliability["2h"]["actual_price_usd"], 104.0)

    def test_performance_summary_aggregates_matured_samples(self) -> None:
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        history = []
        timestamps = []
        closes = []
        for i in range(3):
            origin = base + timedelta(hours=4 * i)
            history.append(
                {
                    "latest_close_at": origin.isoformat(),
                    "latest_close_usd": 100.0 + i,
                    "predictions": {"2h": {"price_usd": 101.0 + i}},
                    "model_predictions": {
                        "persistence": {"2h": {"price_usd": 100.0 + i}}
                    },
                }
            )
            timestamps.append(int((origin + timedelta(hours=2)).timestamp()))
            closes.append(101.5 + i)

        summary = btc_forecast.performance_summary(
            history, np.asarray(closes, dtype=np.float32), timestamps
        )
        self.assertEqual(summary["2h"]["samples"], 3)
        self.assertEqual(summary["2h"]["models"]["persistence"]["samples"], 3)
        self.assertGreater(summary["2h"]["mae_pct"], 0.0)


class ForecastStateTests(unittest.TestCase):
    def test_load_history_supports_versioned_and_legacy_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            with patch.object(btc_forecast, "STATE_PATH", path):
                path.write_text(
                    json.dumps({"version": 3, "forecasts": [{"latest_close_at": "a"}, "bad"]}),
                    encoding="utf-8",
                )
                self.assertEqual(btc_forecast.load_forecast_history(), [{"latest_close_at": "a"}])

                legacy = {"latest_close_at": "b", "predictions": {"2h": {}}}
                path.write_text(json.dumps(legacy), encoding="utf-8")
                self.assertEqual(btc_forecast.load_forecast_history(), [legacy])

    def test_load_history_ignores_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not-json", encoding="utf-8")
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
            with patch.object(btc_forecast, "STATE_PATH", path), patch.object(
                btc_forecast, "HISTORY_LIMIT", 2
            ):
                btc_forecast.save_forecast_history([old, {"latest_close_at": "older"}], output)

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["version"], 3)
            self.assertEqual(len(saved["forecasts"]), 2)
            origins = [item["latest_close_at"] for item in saved["forecasts"]]
            self.assertEqual(origins.count(output["latest_close_at"]), 1)
            self.assertEqual(saved["forecasts"][-1]["latest_close_usd"], 100.0)


if __name__ == "__main__":
    unittest.main()
