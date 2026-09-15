#!/usr/bin/env python3
"""Unit tests for horizon-specific dynamic neutral and no-edge thresholds."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.support.unit_test_stubs import install_timesfm_stub

install_timesfm_stub()

from btc_timesfm.cli import btc_forecast  # noqa: E402
from btc_timesfm.forecasting.dynamic_thresholds import (  # noqa: E402
    Z_95,
    Z_99,
    build_dynamic_threshold_section,
    evaluate_against_fixed,
    horizon_threshold_state,
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
    count: int,
    *,
    horizon: int = 2,
    predicted: float = 2.0,
    actual: float | list[float] = 2.0,
    base: datetime | None = None,
) -> list[dict]:
    start = base or datetime(2026, 9, 1, tzinfo=timezone.utc)
    results: list[dict] = []
    for index in range(count):
        if isinstance(actual, list):
            actual_value = actual[index % len(actual)]
        else:
            actual_value = actual
        results.append(
            row(
                target_at=(start + timedelta(hours=index)).isoformat(),
                horizon=horizon,
                predicted=predicted,
                actual=actual_value,
            )
        )
    return results


VARYING_ACTUALS = [1.5, -1.8, 0.9, -0.4, 2.3, -2.1, 0.7, -0.1]


class HorizonThresholdTests(unittest.TestCase):
    def test_all_horizons_learn_both_thresholds(self) -> None:
        for hour in (2, 4, 8, 16):
            result = horizon_threshold_state(
                matured_rows(30, horizon=hour, actual=VARYING_ACTUALS),
                now=NOW + timedelta(hours=30),
                horizon=hour,
                predicted_change_pct=2.0,
            )
            self.assertEqual(result["horizon"], f"{hour}h")
            self.assertEqual(result["samples"], 30)
            self.assertTrue(result["reliable"])
            self.assertEqual(result["threshold_source"], "learned")
            assert isinstance(result["neutral_threshold_pct"], float)
            assert isinstance(result["no_edge_threshold_pct"], float)
            self.assertGreater(result["no_edge_threshold_pct"], result["neutral_threshold_pct"])
            self.assertIn("edge_status", result)

    def test_noise_band_history_learns_narrower_window(self) -> None:
        low = horizon_threshold_state(
            matured_rows(30, actual=[0.001, -0.001, 0.001, -0.001, 0.001, -0.001, 0.001, -0.001]),
            now=NOW + timedelta(hours=30),
            horizon=2,
            predicted_change_pct=2.0,
        )
        high = horizon_threshold_state(
            matured_rows(30, actual=VARYING_ACTUALS),
            now=NOW + timedelta(hours=30),
            horizon=2,
            predicted_change_pct=2.0,
        )
        self.assertLess(low["neutral_threshold_pct"], high["neutral_threshold_pct"])

    def test_threshold_scales_with_inverse_sqrt_of_sample_count(self) -> None:
        small = horizon_threshold_state(
            matured_rows(20, actual=VARYING_ACTUALS),
            now=NOW + timedelta(hours=20),
            horizon=2,
        )
        large = horizon_threshold_state(
            matured_rows(80, actual=VARYING_ACTUALS),
            now=NOW + timedelta(hours=80),
            horizon=2,
        )
        self.assertGreater(
            float(small["neutral_threshold_pct"]), float(large["neutral_threshold_pct"])
        )

    def test_learned_threshold_matches_formula(self) -> None:
        rows = matured_rows(40, actual=VARYING_ACTUALS)
        state = horizon_threshold_state(rows, now=NOW + timedelta(hours=40), horizon=2)
        import math

        volatility = float(state["realized_volatility_pct"])
        expected_neutral = Z_95 * volatility / math.sqrt(40)
        expected_no_edge = Z_99 * volatility / math.sqrt(40)
        self.assertAlmostEqual(float(state["neutral_threshold_pct"]), expected_neutral, places=6)
        self.assertAlmostEqual(float(state["no_edge_threshold_pct"]), expected_no_edge, places=6)


class LeakageGuardTests(unittest.TestCase):
    def test_future_outcomes_are_excluded(self) -> None:
        matured = [row(target_at="2026-09-06T10:00:00+00:00", predicted=2.0, actual=2.0)]
        future = [row(target_at="2026-09-07T10:00:00+00:00", predicted=2.0, actual=2.0)]
        result = horizon_threshold_state(
            matured + future, now=NOW, horizon=2, predicted_change_pct=2.0
        )
        self.assertEqual(result["samples"], 1)
        self.assertFalse(result["reliable"])
        self.assertEqual(result["threshold_source"], "fixed_fallback")

    def test_other_models_and_horizons_do_not_leak(self) -> None:
        rows_other_model = [
            row(target_at="2026-09-05T00:00:00+00:00", predicted=2.0, actual=2.0, model="ar1")
        ]
        rows_other_horizon = [
            row(target_at="2026-09-05T00:00:00+00:00", predicted=2.0, actual=2.0, horizon=16)
        ]
        result = horizon_threshold_state(
            rows_other_model + rows_other_horizon,
            now=NOW,
            horizon=2,
            predicted_change_pct=2.0,
        )
        self.assertEqual(result["samples"], 0)
        self.assertFalse(result["reliable"])

    def test_build_section_reports_matured_only_cutoff(self) -> None:
        section = build_dynamic_threshold_section(
            matured_rows(25, actual=VARYING_ACTUALS),
            now=NOW + timedelta(hours=25),
        )
        self.assertTrue(section["leakage_guard"]["matured_outcomes_only"])
        self.assertEqual(
            section["leakage_guard"]["target_at_cutoff"], (NOW + timedelta(hours=25)).isoformat()
        )
        self.assertEqual(section["leakage_guard"]["source"], "durable_forecast_history")


class EvidenceSafeguardTests(unittest.TestCase):
    def test_sparse_history_falls_back_to_fixed_threshold(self) -> None:
        result = horizon_threshold_state(
            matured_rows(5, actual=VARYING_ACTUALS),
            now=NOW + timedelta(hours=5),
            horizon=2,
            predicted_change_pct=2.0,
        )
        self.assertFalse(result["reliable"])
        self.assertEqual(result["threshold_source"], "fixed_fallback")
        self.assertEqual(float(result["neutral_threshold_pct"]), 0.25)
        self.assertGreater(result["no_edge_threshold_pct"], result["neutral_threshold_pct"])
        self.assertEqual(result["edge_status"], "insufficient_evidence")
        self.assertTrue(result["suppress_directional_claim"])
        self.assertTrue(any("evidence minimum" in reason for reason in result["suppress_reasons"]))

    def test_empty_history_suppresses_and_is_unreliable(self) -> None:
        result = horizon_threshold_state([], now=NOW, horizon=2, predicted_change_pct=1.0)
        self.assertEqual(result["samples"], 0)
        self.assertFalse(result["reliable"])
        self.assertEqual(result["threshold_source"], "fixed_fallback")
        self.assertTrue(result["suppress_directional_claim"])
        self.assertEqual(
            result["suppress_reasons"][0],
            "only 0 matured samples, below the 20 evidence minimum",
        )

    def test_minimum_evidence_samples_is_configurable(self) -> None:
        result = horizon_threshold_state(
            matured_rows(15, actual=VARYING_ACTUALS),
            now=NOW + timedelta(hours=15),
            horizon=2,
            min_evidence_samples=10,
            predicted_change_pct=2.0,
        )
        self.assertTrue(result["reliable"])

    def test_zero_variance_history_uses_fixed_fallback(self) -> None:
        rows = [
            row(target_at="2026-09-05T00:00:00+00:00", predicted=2.0, actual=0.0),
            row(target_at="2026-09-05T02:00:00+00:00", predicted=2.0, actual=0.0),
            row(target_at="2026-09-05T04:00:00+00:00", predicted=2.0, actual=0.0),
            row(target_at="2026-09-05T06:00:00+00:00", predicted=2.0, actual=0.0),
        ]
        result = horizon_threshold_state(
            rows, now=NOW, horizon=2, predicted_change_pct=1.0, min_evidence_samples=4
        )
        self.assertTrue(result["reliable"])
        self.assertEqual(result["threshold_source"], "fixed_fallback")
        self.assertEqual(float(result["neutral_threshold_pct"]), 0.25)
        self.assertTrue(any("volatility" in reason for reason in result["threshold_reasons"]))

    def test_invalid_configuration_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            horizon_threshold_state([], now=NOW, horizon=2, min_evidence_samples=0)
        with self.assertRaises(ValueError):
            horizon_threshold_state([], now=NOW, horizon=2, fixed_neutral_threshold_pct=-0.1)
        with self.assertRaises(ValueError):
            horizon_threshold_state([], now=NOW, horizon=2, significance_alpha=0.0)
        with self.assertRaises(ValueError):
            horizon_threshold_state(
                [], now=NOW, horizon=2, min_neutral_threshold_pct=2.0, max_neutral_threshold_pct=1.0
            )


class SuppressionTests(unittest.TestCase):
    def test_neutral_prediction_inside_no_edge_band_is_suppressed(self) -> None:
        result = horizon_threshold_state(
            matured_rows(30, actual=VARYING_ACTUALS),
            now=NOW + timedelta(hours=30),
            horizon=2,
            predicted_change_pct=0.0,
        )
        self.assertTrue(result["reliable"])
        self.assertLessEqual(
            abs(float(result["predicted_change_pct"] or 0.0)),
            float(result["no_edge_threshold_pct"]),
        )
        self.assertEqual(result["edge_status"], "no_edge")
        self.assertTrue(result["suppress_directional_claim"])
        self.assertTrue(any("noise band" in reason for reason in result["suppress_reasons"]))

    def test_strong_prediction_outside_noise_band_is_not_suppressed(self) -> None:
        result = horizon_threshold_state(
            matured_rows(30, actual=[1.5, 1.8, 0.9, 1.4, 2.3, 1.1, 0.7, 1.0]),
            now=NOW + timedelta(hours=30),
            horizon=2,
            predicted_change_pct=2.0,
        )
        self.assertTrue(result["reliable"])
        self.assertGreater(
            float(result["predicted_change_pct"]), float(result["no_edge_threshold_pct"])
        )
        self.assertNotEqual(result["edge_status"], "no_edge")
        self.assertFalse(result["suppress_directional_claim"])

    def test_suppress_reasons_explain_claim_block(self) -> None:
        result = horizon_threshold_state(
            [],
            now=NOW,
            horizon=2,
            predicted_change_pct=1.0,
        )
        self.assertTrue(result["suppress_directional_claim"])
        self.assertTrue(result["suppress_reasons"])

    def test_coinflip_probabilities_trigger_no_edge_suppression(self) -> None:
        rows = [
            row(target_at="2026-09-05T00:00:00+00:00", predicted=2.0, actual=1.6),
            row(target_at="2026-09-05T02:00:00+00:00", predicted=2.0, actual=-1.7),
            row(target_at="2026-09-05T04:00:00+00:00", predicted=2.0, actual=1.5),
            row(target_at="2026-09-05T06:00:00+00:00", predicted=2.0, actual=-1.6),
        ]
        result = horizon_threshold_state(
            rows, now=NOW, horizon=2, predicted_change_pct=1.0, min_evidence_samples=4
        )
        self.assertEqual(result["edge_status"], "no_edge")
        self.assertTrue(result["suppress_directional_claim"])

    def test_public_directional_claim_blocked_when_any_horizon_suppressed(self) -> None:
        mature = matured_rows(30, actual=VARYING_ACTUALS)
        for _ in range(20):
            mature.append(
                row(target_at="2026-09-20T00:00:00+00:00", horizon=16, predicted=2.0, actual=2.0)
            )
        section = build_dynamic_threshold_section(
            mature,
            now=NOW + timedelta(hours=30),
            predictions={"2h": {"change_pct": 2.0}, "16h": {"change_pct": 2.0}},
        )
        self.assertTrue(section["horizons"]["2h"]["reliable"])
        self.assertFalse(section["horizons"]["16h"]["reliable"])
        self.assertFalse(section["public_directional_claim_allowed"])
        self.assertIn("16h", section["suppressed_horizons"])


class FixedBaselineEvaluationTests(unittest.TestCase):
    def test_evaluate_against_fixed_returns_comparison(self) -> None:
        rows_data = matured_rows(30, actual=VARYING_ACTUALS)
        rows_tuples = [
            (float(r["predicted_change_pct"]), float(r["actual_change_pct"])) for r in rows_data
        ]
        result = evaluate_against_fixed(rows_tuples, neutral_threshold_pct=1.5)
        self.assertEqual(result["samples"], 30)
        self.assertEqual(result["fixed_threshold_pct"], 0.25)
        self.assertEqual(result["dynamic_threshold_pct"], 1.5)
        self.assertIsInstance(result["fixed_neutral_rate"], float)
        self.assertIsInstance(result["dynamic_neutral_rate"], float)
        self.assertLessEqual(result["fixed_neutral_rate"], result["dynamic_neutral_rate"])
        self.assertGreaterEqual(result["agreement_rate"], 0.0)
        self.assertGreaterEqual(result["reclassified_directional_to_neutral"], 0)
        self.assertGreaterEqual(result["reclassified_neutral_to_directional"], 0)
        self.assertIsInstance(result["fixed_direction_accuracy"], float)
        self.assertIsInstance(result["dynamic_direction_accuracy"], float)

    def test_evaluate_against_fixed_empty_rows(self) -> None:
        result = evaluate_against_fixed([], neutral_threshold_pct=1.0)
        self.assertEqual(result["samples"], 0)
        self.assertIsNone(result["fixed_neutral_rate"])
        self.assertIsNone(result["dynamic_neutral_rate"])

    def test_evaluate_rejects_negative_thresholds(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_against_fixed([], neutral_threshold_pct=-0.1)
        with self.assertRaises(ValueError):
            evaluate_against_fixed([], neutral_threshold_pct=1.0, fixed_threshold_pct=-0.1)

    def test_base_line_section_embedded_in_state(self) -> None:
        state = horizon_threshold_state(
            matured_rows(30, actual=VARYING_ACTUALS),
            now=NOW + timedelta(hours=30),
            horizon=2,
            predicted_change_pct=2.0,
        )
        self.assertEqual(state["baseline_evaluation"]["samples"], 30)
        self.assertIn("fixed_threshold_pct", state["baseline_evaluation"])


class JsonExposureTests(unittest.TestCase):
    def test_section_exposes_threshold_state_for_all_horizons(self) -> None:
        section = build_dynamic_threshold_section(
            matured_rows(30, actual=VARYING_ACTUALS),
            now=NOW + timedelta(hours=30),
            predictions={"2h": {"change_pct": 1.0}, "4h": {"change_pct": 1.0}},
        )
        self.assertEqual(set(section["horizons"]), {"2h", "4h", "8h", "16h"})
        self.assertEqual(section["version"], 1)
        self.assertIn("neutral_threshold_pct", section["horizons"]["2h"])
        self.assertIn("no_edge_threshold_pct", section["horizons"]["2h"])
        self.assertIn("threshold_source", section["horizons"]["2h"])
        self.assertIn("edge_status", section["horizons"]["2h"])
        self.assertIn("suppress_directional_claim", section["horizons"]["2h"])
        self.assertIn("statistical_evidence", section["horizons"]["2h"])
        self.assertIn("calibrated_evidence", section["horizons"]["2h"])
        self.assertIn("baseline_evaluation", section["horizons"]["2h"])
        json.dumps(section)

    def test_mean_realized_volatility_aggregate(self) -> None:
        section = build_dynamic_threshold_section(
            matured_rows(30, actual=VARYING_ACTUALS),
            now=NOW + timedelta(hours=30),
        )
        self.assertGreater(section["overall"]["mean_realized_volatility_pct"], 0.0)

    def test_section_is_json_serializable_with_leakage_guard(self) -> None:
        section = build_dynamic_threshold_section([], now=NOW)
        self.assertEqual(section["overall"]["horizon_count"], 4)
        self.assertTrue(section["leakage_guard"]["matured_outcomes_only"])
        json.dumps(section)

    def test_forecast_json_embedding_via_cli_helper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "history.sqlite"
            store = ForecastHistoryStore(db)
            origin = datetime(2026, 9, 1, tzinfo=timezone.utc)
            actuals: dict[int, float] = {}
            for index in range(30):
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
            section = btc_forecast.build_dynamic_threshold(
                store, predictions, origin + timedelta(hours=30)
            )
            self.assertEqual(section["version"], 1)
            self.assertEqual(set(section["horizons"]), {"2h", "4h", "8h", "16h"})
            self.assertTrue(section["horizons"]["2h"]["reliable"])
            json.dumps(section)


if __name__ == "__main__":
    unittest.main()
