#!/usr/bin/env python3
"""Unit tests for calibrated probability-of-direction forecasts."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.support.unit_test_stubs import install_timesfm_stub

install_timesfm_stub()

from btc_timesfm.cli import btc_forecast  # noqa: E402
from btc_timesfm.forecasting.direction_probability import (  # noqa: E402
    build_forecast_probabilities,
    classify_direction,
    horizon_probabilities,
)
from btc_timesfm.history.history_store import ForecastHistoryStore  # noqa: E402

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def row(
    *,
    target_at: str,
    horizon: int = 2,
    predicted: float,
    actual: float,
    model: str = "ensemble",
) -> dict[str, object]:
    return {
        "model_name": model,
        "horizon_hours": horizon,
        "target_at": target_at,
        "origin_at": target_at,
        "predicted_change_pct": predicted,
        "actual_change_pct": actual,
        "actual_target_price_usd": 100.0 + actual,
        "regime": "range",
    }


def matured_rows(
    count: int, *, horizon: int = 2, predicted: float = 2.0, actual: float = 2.0
) -> list[dict]:
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return [
        row(
            target_at=(base + timedelta(hours=index)).isoformat(),
            horizon=horizon,
            predicted=predicted,
            actual=actual,
        )
        for index in range(count)
    ]


class DirectionProbabilityTests(unittest.TestCase):
    def test_sparse_history_falls_back_to_drift(self) -> None:
        result = horizon_probabilities([], now=NOW, horizon=2)
        self.assertEqual(result["samples"], 0)
        self.assertFalse(result["reliable"])
        self.assertEqual(result["calibration_state"], "sparse_fallback")
        self.assertEqual(result["p_up"], 0.5)
        self.assertEqual(result["p_down"], 0.5)
        self.assertEqual(result["p_move"], 0.5)
        self.assertIsNone(result["brier_score"])
        self.assertIn("no matured samples", result["reasons"][0])

    def test_sparse_history_abandons_naive_frequency_for_drift(self) -> None:
        result = horizon_probabilities(
            matured_rows(3, actual=2.0),
            now=NOW + timedelta(hours=2),
            horizon=2,
        )
        self.assertFalse(result["reliable"])
        self.assertEqual(result["calibration_state"], "sparse_fallback")
        self.assertLess(result["p_up"], 1.0)
        self.assertGreater(result["p_up"], 0.5)

    def test_perfect_classifier_calibrates_and_achieves_low_brier(self) -> None:
        result = horizon_probabilities(
            matured_rows(30, predicted=2.0, actual=2.0),
            now=NOW + timedelta(hours=30),
            horizon=2,
            predicted_change_pct=1.0,
        )
        self.assertTrue(result["reliable"])
        self.assertEqual(result["calibration_state"], "calibrated")
        self.assertGreater(result["p_up"], 0.9)
        self.assertLess(result["p_down"], 0.1)
        self.assertEqual(result["samples"], 30)
        self.assertIsNotNone(result["brier_score"])
        assert result["brier_score"] is not None
        self.assertLess(result["brier_score"], 0.01)

    def test_leakage_guard_excludes_future_outcomes(self) -> None:
        rows = [
            row(target_at="2026-09-06T10:00:00+00:00", predicted=2.0, actual=2.0),
            row(target_at="2026-09-06T13:00:00+00:00", predicted=2.0, actual=2.0),
        ]
        result = horizon_probabilities(rows, now=NOW, horizon=2)
        self.assertEqual(result["all_samples"], 1)
        self.assertEqual(result["samples"], 1)

    def test_marginal_and_conditioned_cohorts_differ(self) -> None:
        rows = [
            row(target_at="2026-09-06T00:00:00+00:00", predicted=2.0, actual=2.0),
            row(target_at="2026-09-06T02:00:00+00:00", predicted=2.0, actual=2.0),
            row(target_at="2026-09-06T04:00:00+00:00", predicted=-2.0, actual=-2.0),
        ]
        marginal = horizon_probabilities(rows, now=NOW, horizon=2, predicted_change_pct=None)
        conditioned = horizon_probabilities(rows, now=NOW, horizon=2, predicted_change_pct=1.0)
        self.assertEqual(marginal["samples"], 3)
        self.assertEqual(conditioned["samples"], 2)
        self.assertGreater(conditioned["p_up"], marginal["p_up"])

    def test_reliability_diagnostics_shape(self) -> None:
        result = horizon_probabilities(
            matured_rows(30, horizon=4, actual=2.0),
            now=NOW + timedelta(hours=30),
            horizon=4,
        )
        reliability = result["reliability"]
        self.assertEqual(len(reliability["bins"]), 10)
        for binned in reliability["bins"]:
            self.assertIn("bin", binned)
            self.assertIn("count", binned)
            self.assertIn("predicted_frequency", binned)
            self.assertIn("observed_frequency", binned)
        self.assertIsInstance(result["calibration_error"], (int, float))
        self.assertGreaterEqual(result["calibration_error"], 0.0)
        self.assertGreaterEqual(result["occupied_bins"], 1)

    def test_forecast_probability_section_is_json_serializable(self) -> None:
        section = build_forecast_probabilities(
            matured_rows(30, actual=2.0),
            now=NOW + timedelta(hours=30),
            predictions={"2h": {"change_pct": 1.0}},
        )
        self.assertIn("2h", section["horizons"])
        self.assertIn("public_claim_allowed", section)
        self.assertTrue(section["leakage_guard"]["matured_outcomes_only"])
        json.dumps(section)

    def test_public_claim_blocked_when_any_horizon_lacks_evidence(self) -> None:
        rows = matured_rows(25, actual=2.0)
        for _ in range(24):
            rows.append(
                row(target_at="2026-09-20T00:00:00+00:00", horizon=16, predicted=2.0, actual=2.0)
            )
        section = build_forecast_probabilities(
            rows,
            now=NOW + timedelta(hours=25),
            predictions={"2h": {"change_pct": 1.0}, "16h": {"change_pct": 1.0}},
        )
        self.assertTrue(section["horizons"]["2h"]["reliable"])
        self.assertFalse(section["horizons"]["16h"]["reliable"])
        self.assertFalse(section["public_claim_allowed"])

    def test_classify_direction_rejects_negative_threshold(self) -> None:
        self.assertEqual(classify_direction(0.5, 0.25), "up")
        self.assertEqual(classify_direction(-0.5, 0.25), "down")
        self.assertEqual(classify_direction(0.1, 0.25), "neutral")
        with self.assertRaises(ValueError):
            classify_direction(0.0, -0.1)

    def test_invalid_configuration_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_forecast_probabilities([], move_threshold_pct=-0.1)
        with self.assertRaises(ValueError):
            build_forecast_probabilities([], min_evidence_samples=0)
        with self.assertRaises(ValueError):
            build_forecast_probabilities([], shrinkage_strength=0.0)

    def test_forecast_json_embedding_via_cli_helper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "history.sqlite"
            store = ForecastHistoryStore(db)
            origin = datetime(2026, 9, 1, tzinfo=timezone.utc)
            actuals: dict[int, float] = {}
            for index in range(25):
                current_origin = origin + timedelta(hours=index)
                source_price = 100.0
                forecast_price = 102.0
                snapshot = {
                    "generated_at": current_origin.isoformat(),
                    "latest_close_at": current_origin.isoformat(),
                    "latest_close_usd": source_price,
                    "source": "test",
                    "pair": "BTCUSD",
                    "regime": "range",
                    "market_features": {},
                    "predictions": {
                        f"{hour}h": {"price_usd": forecast_price} for hour in (2, 4, 8, 16)
                    },
                }
                for hour in (2, 4, 8, 16):
                    target_at = current_origin + timedelta(hours=hour)
                    actuals[int(target_at.timestamp())] = 101.0
                store.ingest_snapshot(snapshot)
            store.enrich_outcomes(actuals)

            predictions = {f"{hour}h": {"change_pct": 2.0} for hour in (2, 4, 8, 16)}
            section = btc_forecast.build_direction_probability(
                store, predictions, origin + timedelta(hours=24, minutes=59)
            )
            self.assertEqual(section["version"], 1)
            self.assertEqual(set(section["horizons"]), {"2h", "4h", "8h", "16h"})
            self.assertTrue(section["horizons"]["2h"]["reliable"])
            json.dumps(section)

    def test_embedding_helper_threads_module_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "history.sqlite"
            store = ForecastHistoryStore(db)
            store.ingest_snapshot(
                {
                    "generated_at": "2026-09-01T00:00:00+00:00",
                    "latest_close_at": "2026-09-01T00:00:00+00:00",
                    "latest_close_usd": 100.0,
                    "source": "test",
                    "pair": "BTCUSD",
                    "regime": "range",
                    "market_features": {},
                    "predictions": {"2h": {"price_usd": 102.0, "change_pct": 2.0}},
                },
                {int(datetime(2026, 9, 1, 2, tzinfo=timezone.utc).timestamp()): 101.0},
            )
            section = btc_forecast.build_direction_probability(
                store,
                {"2h": {"change_pct": 2.0}},
                datetime(2026, 9, 1, 1, tzinfo=timezone.utc),
            )
            self.assertIn("2h", section["horizons"])
            self.assertEqual(
                section["horizons"]["2h"]["predicted_direction"],
                classify_direction(2.0, 0.25),
            )


if __name__ == "__main__":
    unittest.main()
