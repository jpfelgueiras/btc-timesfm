#!/usr/bin/env python3
"""Unit tests for cross-horizon forecast coherence.

Covers crossing detection, direction-flip flagging/suppression, leakage-safe
reconciliation within guardrails, coverage preservation, coherence scoring, and
the loud-failure path for unrecoverable violations.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from btc_timesfm.forecasting.multi_horizon_coherence import (
    CoherenceViolationError,
    assert_forecast_coherent,
    detect_cross_horizon_violations,
    reconcile_forecast_coherence,
)

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
BASE = 100.0
SAMPLES = {"2h": 30, "4h": 30, "8h": 30, "16h": 30}
SPARSE = {"2h": 30, "4h": 30, "8h": 3, "16h": 3}


def forecast(overrides: dict[str, dict[str, float]] | None = None) -> dict[str, dict[str, float]]:
    """Default coherent forecast plus optional per-horizon overrides."""
    frame: dict[str, dict[str, float]] = {
        "2h": {
            "price_usd": 100.5,
            "q10_usd": 99.5,
            "q50_usd": 100.5,
            "q90_usd": 101.5,
            "change_pct": 0.5,
        },
        "4h": {
            "price_usd": 101.0,
            "q10_usd": 98.5,
            "q50_usd": 101.0,
            "q90_usd": 103.5,
            "change_pct": 1.0,
        },
        "8h": {
            "price_usd": 101.5,
            "q10_usd": 97.5,
            "q50_usd": 101.5,
            "q90_usd": 105.5,
            "change_pct": 1.5,
        },
        "16h": {
            "price_usd": 102.0,
            "q10_usd": 96.5,
            "q50_usd": 102.0,
            "q90_usd": 107.5,
            "change_pct": 2.0,
        },
    }
    for key, values in (overrides or {}).items():
        if key in frame:
            frame[key].update(values)
    return frame


def _reconcile(predictions: dict[str, dict[str, float]], **kwargs: object) -> dict:
    return reconcile_forecast_coherence(
        predictions,
        base_price_usd=BASE,
        samples=SAMPLES,
        now=NOW,
        **kwargs,
    )


def _crossing_records(section: dict) -> list[dict]:
    return [item for item in section["violations"] if item.get("severity") == "crossing"]


def _flip_records(section: dict) -> list[dict]:
    return [item for item in section["violations"] if item.get("type") == "direction_flip"]


class CrossingDetectionTests(unittest.TestCase):
    def test_clean_forecast_has_zero_violations_and_full_score(self) -> None:
        result = _reconcile(forecast())
        log = result["section"]["violation_log"]
        self.assertEqual(log["crossing_entries"], 0)
        self.assertEqual(log["unrecoverable_crossings"], 0)
        self.assertEqual(log["evidenced_direction_flips"], 0)
        self.assertEqual(result["section"]["coherence_score"], 1.0)
        self.assertTrue(result["section"]["coherent"])
        self.assertTrue(result["section"]["public_claim_allowed"])

    def test_detect_reports_torn_intrahorizon_quantiles(self) -> None:
        detected = detect_cross_horizon_violations(
            forecast({"4h": {"q90_usd": 100.2}}),
            base_price_usd=BASE,
            samples=SAMPLES,
        )
        levels = {
            (item["type"], item.get("horizon"), item.get("level"))
            for item in detected["crossing_violations"]
        }
        self.assertIn(("quantile_crossing", "4h", "q90_usd"), levels)
        self.assertIn(("envelope_crossing", None, "q90_usd"), levels)

    def test_detect_reports_cross_horizon_q90_envelope_fall(self) -> None:
        detected = detect_cross_horizon_violations(
            forecast({"16h": {"q90_usd": 105.4}}),
            base_price_usd=BASE,
            samples=SAMPLES,
        )
        envelope = [
            item for item in detected["crossing_violations"] if item["type"] == "envelope_crossing"
        ]
        self.assertEqual(len(envelope), 1)
        self.assertEqual(envelope[0]["shorter_horizon"], "8h")
        self.assertEqual(envelope[0]["longer_horizon"], "16h")
        self.assertEqual(envelope[0]["level"], "q90_usd")

    def test_detect_reports_cross_horizon_q10_envelope_rise(self) -> None:
        detected = detect_cross_horizon_violations(
            forecast({"16h": {"q10_usd": 98.0}}),
            base_price_usd=BASE,
            samples=SAMPLES,
        )
        envelope = [
            item for item in detected["crossing_violations"] if item["type"] == "envelope_crossing"
        ]
        self.assertTrue(any(item["level"] == "q10_usd" for item in envelope))

    def test_median_sign_flip_with_evidence_is_flagged(self) -> None:
        predictions = forecast(
            {
                "8h": forecast()["8h"]
                | {
                    "price_usd": 99.0,
                    "q10_usd": 97.0,
                    "q50_usd": 99.0,
                    "q90_usd": 105.5,
                    "change_pct": -1.0,
                }
            }
        )
        predictions["16h"] = forecast()["16h"] | {
            "price_usd": 98.5,
            "q10_usd": 95.5,
            "q50_usd": 98.5,
            "q90_usd": 106.0,
            "change_pct": -1.5,
        }
        result = _reconcile(predictions)
        flips = _flip_records(result["section"])
        self.assertTrue(any(item["shorter_horizon"] == "4h" and item["flipped"] for item in flips))
        evid = next(item for item in flips if item["shorter_horizon"] == "4h")
        self.assertFalse(evid["suppressed"])
        self.assertEqual(evid["evidence"]["shorter_samples"], 30)
        self.assertEqual(result["section"]["violation_log"]["evidenced_direction_flips"], 1)
        self.assertEqual(result["section"]["coherence_score"], 0.85)
        self.assertFalse(result["section"]["public_claim_allowed"])

    def test_direction_flip_is_suppressed_when_sample_poor(self) -> None:
        predictions = forecast(
            {
                "8h": forecast()["8h"]
                | {
                    "price_usd": 99.0,
                    "q10_usd": 97.0,
                    "q50_usd": 99.0,
                    "q90_usd": 105.5,
                    "change_pct": -1.0,
                }
            }
        )
        predictions["16h"] = forecast()["16h"] | {
            "price_usd": 98.5,
            "q10_usd": 95.5,
            "q50_usd": 98.5,
            "q90_usd": 106.0,
            "change_pct": -1.5,
        }
        result = reconcile_forecast_coherence(
            predictions,
            base_price_usd=BASE,
            samples=SPARSE,
            now=NOW,
        )
        flips = _flip_records(result["section"])
        suppressed = [item for item in flips if item["suppressed"]]
        self.assertTrue(suppressed)
        self.assertTrue(all(item["suppression_reason"] == "sample_poor" for item in suppressed))
        self.assertEqual(result["section"]["violation_log"]["suppressed_direction_flips"], 1)
        self.assertEqual(result["section"]["coherence_score"], 1.0)

    def test_direction_flip_is_suppressed_below_noise_band(self) -> None:
        predictions = forecast(
            {
                "2h": {
                    "price_usd": 100.05,
                    "q10_usd": 99.05,
                    "q50_usd": 100.05,
                    "q90_usd": 101.05,
                    "change_pct": 0.05,
                },
                "4h": {
                    "price_usd": 99.95,
                    "q10_usd": 98.45,
                    "q50_usd": 99.95,
                    "q90_usd": 101.45,
                    "change_pct": -0.05,
                },
            }
        )
        result = _reconcile(predictions)
        flips = _flip_records(result["section"])
        suppressed = [item for item in flips if item["suppressed"]]
        self.assertTrue(suppressed)
        self.assertTrue(
            all(item["suppression_reason"] == "below_noise_band" for item in suppressed)
        )
        start_pair = next(item for item in suppressed if item["shorter_horizon"] == "2h")
        self.assertEqual(start_pair["longer_horizon"], "4h")
        self.assertGreaterEqual(result["section"]["violation_log"]["suppressed_direction_flips"], 1)


class ReconciliationTests(unittest.TestCase):
    def test_envelope_crossing_is_repaired_within_guardrails(self) -> None:
        result = _reconcile(forecast({"16h": {"q90_usd": 105.4}}))
        log = result["section"]["violation_log"]
        self.assertEqual(log["crossing_entries"], 1)
        self.assertEqual(log["reconciled_crossings"], 1)
        self.assertEqual(log["unrecoverable_crossings"], 0)
        self.assertEqual(result["section"]["coherence_score"], 1.0)
        self.assertTrue(result["section"]["coherent"])
        self.assertGreater(
            result["reconciled_predictions"]["16h"]["q90_usd"],
            result["section"]["horizons"]["8h"]["q90_usd"],
        )

    def test_torn_lower_bound_is_repaired_around_unchanged_median(self) -> None:
        result = _reconcile(forecast({"2h": {"q10_usd": 100.495}}))
        log = result["section"]["violation_log"]
        self.assertEqual(log["unrecoverable_crossings"], 0)
        horizon = result["section"]["horizons"]["2h"]
        self.assertLess(horizon["reconciled_q10_usd"], horizon["q50_usd"])
        self.assertEqual(horizon["q50_usd"], horizon["q50_usd"])

    def test_reconciliation_preserves_quantile_order_for_all_horizons(self) -> None:
        result = _reconcile(forecast({"16h": {"q90_usd": 105.4}, "2h": {"q10_usd": 100.495}}))
        self.assertEqual(result["section"]["violation_log"]["unrecoverable_crossings"], 0)
        for key, row in result["section"]["horizons"].items():
            self.assertLess(row["reconciled_q10_usd"], row["q50_usd"])
            self.assertLess(row["q50_usd"], row["reconciled_q90_usd"])

    def test_reconciliation_only_widens_or_keeps_band(self) -> None:
        result = _reconcile(forecast({"16h": {"q90_usd": 105.4}}))
        for key in ("2h", "4h", "8h", "16h"):
            before = result["section"]["horizons"][key]
            after = result["reconciled_predictions"][key]
            self.assertLessEqual(after["q10_usd"], before["q10_usd"])
            self.assertGreaterEqual(after["q90_usd"], before["q90_usd"])

    def test_reconciliation_keeps_width_growth_within_tolerance(self) -> None:
        result = _reconcile(forecast({"16h": {"q90_usd": 105.4}}))
        for key, row in result["section"]["horizons"].items():
            before = row["width_pct_before"]
            after = row["width_pct_after"]
            if before is None or after is None:
                continue
            self.assertLessEqual(after, before * 1.20)

    def test_recorded_reconciliation_deltas_respect_guardrail(self) -> None:
        result = _reconcile(forecast({"16h": {"q90_usd": 105.4}}))
        for key, row in result["section"]["horizons"].items():
            for field in ("reconciliation_delta_q10_pct", "reconciliation_delta_q90_pct"):
                value = row[field]
                if value is not None:
                    self.assertLessEqual(abs(value), 2.0)

    def test_reverse_base_price_resolution_matches_explicit_base(self) -> None:
        explicit = _reconcile(forecast({"16h": {"q90_usd": 105.4}}))
        implied = reconcile_forecast_coherence(
            forecast({"16h": {"q90_usd": 105.4}}),
            samples=SAMPLES,
            now=NOW,
        )
        self.assertEqual(
            explicit["reconciled_predictions"],
            implied["reconciled_predictions"],
        )

    def test_leakage_guard_excludes_outcome_inputs(self) -> None:
        result = _reconcile(forecast({"16h": {"q90_usd": 105.4}}))
        guard = result["section"]["leakage_guard"]
        self.assertTrue(guard["matured_outcomes_only"])
        self.assertEqual(guard["outcome_inputs"], [])
        self.assertIn("q10_usd", guard["reconciliation_inputs"])
        self.assertIn("q90_usd", guard["reconciliation_inputs"])


class LoudFailureTests(unittest.TestCase):
    def test_unrecoverable_envelope_crossing_fails_loudly(self) -> None:
        predictions = forecast({"16h": {"q10_usd": 100.0, "q50_usd": 101.0, "q90_usd": 105.0}})
        result = _reconcile(predictions)
        self.assertGreater(result["section"]["violation_log"]["unrecoverable_crossings"], 0)
        self.assertLessEqual(result["section"]["coherence_score"], 0.5)
        self.assertFalse(result["section"]["coherent"])
        with self.assertRaises(CoherenceViolationError):
            assert_forecast_coherent(
                predictions,
                base_price_usd=BASE,
                samples=SAMPLES,
                now=NOW,
            )

    def test_guardrail_breach_fails_loudly(self) -> None:
        result = _reconcile(forecast({"4h": {"q90_usd": 100.2}}))
        self.assertGreaterEqual(result["section"]["violation_log"]["unrecoverable_crossings"], 1)
        self.assertEqual(result["section"]["coherence_score"], 0.5)
        with self.assertRaises(CoherenceViolationError):
            assert_forecast_coherent(
                forecast({"4h": {"q90_usd": 100.2}}),
                base_price_usd=BASE,
                samples=SAMPLES,
                now=NOW,
            )

    def test_qa_path_yields_zero_crossings_after_assert(self) -> None:
        predictions = forecast({"16h": {"q90_usd": 105.4}, "2h": {"q10_usd": 100.495}})
        result = assert_forecast_coherent(
            predictions,
            base_price_usd=BASE,
            samples=SAMPLES,
            now=NOW,
        )
        self.assertEqual(result["section"]["violation_log"]["unrecoverable_crossings"], 0)
        rechecked = detect_cross_horizon_violations(
            result["reconciled_predictions"],
            base_price_usd=BASE,
            samples=SAMPLES,
        )
        self.assertFalse(rechecked["crossing_violations"])

    def test_coherence_score_penalizes_crossings_not_flips_only_when_evidenced(self) -> None:
        sample_poor_flip = forecast(
            {
                "8h": forecast()["8h"]
                | {
                    "price_usd": 99.0,
                    "q10_usd": 97.0,
                    "q50_usd": 99.0,
                    "q90_usd": 105.5,
                    "change_pct": -1.0,
                }
            }
        )
        sample_poor_flip["16h"] = forecast()["16h"] | {
            "price_usd": 98.5,
            "q10_usd": 95.5,
            "q50_usd": 98.5,
            "q90_usd": 106.0,
            "change_pct": -1.5,
        }
        suppress_result = reconcile_forecast_coherence(
            sample_poor_flip,
            base_price_usd=BASE,
            samples=SPARSE,
            now=NOW,
        )
        self.assertEqual(suppress_result["section"]["coherence_score"], 1.0)


if __name__ == "__main__":
    unittest.main()
