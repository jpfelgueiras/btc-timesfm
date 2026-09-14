#!/usr/bin/env python3
"""Tests for feature freshness and decay analysis."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from btc_timesfm.research.feature_freshness_analysis import (
    DEFAULT_AGE_BAND_EDGES_HOURS,
    build_freshness_report,
    default_stale_threshold_hours,
    evaluate_value_vs_age,
    evidence_based_threshold,
    feature_ages,
    render_markdown,
    stale_signal_policy,
)

NOW = datetime(2026, 1, 6, 0, 0, tzinfo=timezone.utc)


def _iso(day: int, hour: int, minute: int = 0) -> str:
    return datetime(2026, 1, day, hour, minute, tzinfo=timezone.utc).isoformat()


def matured_row(
    *,
    origin_at: str,
    horizon: int,
    feature_name: str,
    observation_at: str,
    feature_value: float = 0.1,
    mae: float = 1.0,
    direction: int = 1,
    actual_change: float = 1.0,
    model: str = "ensemble",
    target_at: str | None = None,
) -> dict[str, Any]:
    features: dict[str, Any] = {
        feature_name: feature_value,
        "feature_observation_times": {feature_name: observation_at},
    }
    origin = datetime.fromisoformat(origin_at)
    if target_at is None:
        target_at = (origin + timedelta(hours=horizon)).isoformat()
    return {
        "origin_at": origin_at,
        "horizon_hours": horizon,
        "target_at": target_at,
        "model_name": model,
        "market_features_json": json.dumps(features),
        "actual_target_price_usd": 100.0,
        "absolute_error_pct": mae,
        "direction_correct": direction,
        "actual_change_pct": actual_change,
    }


def band(
    start: float,
    end: float | None,
    samples: int,
    mae: float,
    baseline_mae: float,
    *,
    conclusive: bool = True,
) -> dict[str, Any]:
    return {
        "age_band": "test",
        "age_band_start_hours": start,
        "age_band_end_hours": end,
        "samples": samples,
        "mae_pct": mae,
        "baseline_mae_pct": baseline_mae,
        "direction_accuracy": 0.6,
        "conclusive": conclusive,
    }


class FeatureAgesTests(unittest.TestCase):
    def test_feature_ages_is_deterministic_and_skips_unrecorded_ages(self) -> None:
        features = {
            "derivatives_funding_rate_pct": 0.01,
            "derivatives_open_interest_usd": 1_000_000.0,
            "volatility_24h_pct": 0.7,
            "feature_observation_times": {
                "derivatives_funding_rate_pct": _iso(1, 0, 0),
                "derivatives_open_interest_usd": _iso(1, 1, 30),
            },
        }
        origin = _iso(1, 4, 0)
        first = feature_ages(features, origin)
        second = feature_ages(features, origin)
        self.assertEqual(first, second)
        self.assertEqual(first["derivatives_funding_rate_pct"], 4.0)
        self.assertEqual(first["derivatives_open_interest_usd"], 2.5)
        self.assertNotIn("volatility_24h_pct", first)

    def test_feature_ages_supports_global_capture_and_epoch_timestamps(self) -> None:
        globals_features = {
            "rsi_14": 50.0,
            "volatility_24h_pct": 1.0,
            "captured_at": _iso(1, 0, 0),
        }
        ages = feature_ages(globals_features, _iso(1, 2, 0))
        self.assertEqual(ages["rsi_14"], 2.0)
        self.assertEqual(ages["volatility_24h_pct"], 2.0)

        epoch = int(datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc).timestamp())
        epoch_features = {
            "rsi_14": 50.0,
            "feature_timestamps": {"rsi_14": epoch},
        }
        self.assertEqual(feature_ages(epoch_features, _iso(1, 3, 0))["rsi_14"], 3.0)


class ValueVsAgeTests(unittest.TestCase):
    def test_leakage_safe_bucketing_excludes_future_rows(self) -> None:
        rows = [
            matured_row(
                origin_at=_iso(5, i), horizon=4, feature_name="f", observation_at=_iso(5, 0, i)
            )
            for i in range(3)
        ]
        future_row = matured_row(
            origin_at=_iso(5, 10, 0),
            observation_at=_iso(5, 0, 0),
            horizon=4,
            feature_name="f",
            target_at=_iso(6, 2, 0),
        )
        report = build_freshness_report(rows + [future_row], NOW, min_samples=2)
        self.assertEqual(report["samples"]["excluded_future_rows"], 1)
        self.assertEqual(report["samples"]["evaluation_rows"], 3)
        curve = report["curves"]["f"]["4h"]
        self.assertEqual(curve["samples"], 3)

    def test_value_vs_age_curve_shape_and_low_sample_inconclusive(self) -> None:
        rows = []
        for i in range(6):
            rows.append(
                matured_row(
                    origin_at=_iso(3, 0, i),
                    horizon=4,
                    feature_name="derivatives_funding_rate_pct",
                    observation_at=_iso(3, 0, 0),
                )
            )
        for i in range(3):
            rows.append(
                matured_row(
                    origin_at=_iso(3, 21, i),
                    horizon=4,
                    feature_name="derivatives_funding_rate_pct",
                    observation_at=_iso(3, 1, 0),
                )
            )
        curves, effective = evaluate_value_vs_age(rows, now=NOW, min_samples=5)
        self.assertEqual(effective, "ensemble")
        curve = curves["derivatives_funding_rate_pct"]["4h"]
        self.assertEqual(curve["samples"], 9)
        self.assertEqual(len(curve["curve"]), len(DEFAULT_AGE_BAND_EDGES_HOURS))
        fresh_band = next(b for b in curve["curve"] if b["samples"] == 6)
        self.assertTrue(fresh_band["conclusive"])
        self.assertIsNotNone(fresh_band["mae_pct"])
        inconclusive = next(b for b in curve["curve"] if b["samples"] == 3)
        self.assertFalse(inconclusive["conclusive"])
        self.assertIsNone(inconclusive["mae_pct"])
        self.assertIsNone(inconclusive["direction_accuracy"])
        self.assertIsNone(inconclusive["baseline_mae_pct"])


class EvidenceThresholdTests(unittest.TestCase):
    def test_evidence_based_threshold_uses_first_degraded_band_start(self) -> None:
        curve = [
            band(0.0, 1.0, 40, mae=0.8, baseline_mae=2.0),
            band(12.0, 24.0, 40, mae=2.4, baseline_mae=2.0),
            band(24.0, None, 40, mae=3.0, baseline_mae=2.0),
        ]
        evidence = evidence_based_threshold(curve, min_samples=5, default_threshold_hours=2.0)
        self.assertTrue(evidence["evidence_based"])
        self.assertEqual(evidence["threshold_hours"], 12.0)
        self.assertEqual(evidence["action"], "exclude")
        self.assertEqual(evidence["questionable_samples"], 40)
        self.assertEqual(evidence["evidence_samples"], 120)

    def test_evidence_based_threshold_downgrades_within_tolerance(self) -> None:
        curve = [
            band(0.0, 1.0, 40, mae=0.8, baseline_mae=2.0),
            band(1.0, 2.0, 40, mae=2.05, baseline_mae=2.0),
        ]
        evidence = evidence_based_threshold(curve, min_samples=5, default_threshold_hours=2.0)
        self.assertEqual(evidence["action"], "downgrade")
        self.assertEqual(evidence["threshold_hours"], 1.0)

    def test_insufficient_evidence_keeps_deterministic_default(self) -> None:
        curve = [band(12.0, 24.0, 40, mae=2.4, baseline_mae=2.0)]
        evidence = evidence_based_threshold(curve, min_samples=5, default_threshold_hours=3.0)
        self.assertFalse(evidence["evidence_based"])
        self.assertEqual(evidence["threshold_hours"], 3.0)
        self.assertEqual(evidence["action"], "keep")

    def test_never_degraded_threshold_extends_to_observed_range(self) -> None:
        curve = [
            band(0.0, 1.0, 40, mae=0.5, baseline_mae=2.0),
            band(1.0, 2.0, 40, mae=0.8, baseline_mae=2.0),
        ]
        evidence = evidence_based_threshold(curve, min_samples=5, default_threshold_hours=1.0)
        self.assertTrue(evidence["evidence_based"])
        self.assertEqual(evidence["threshold_hours"], 2.0)
        self.assertEqual(evidence["action"], "keep")


class StalePolicyTests(unittest.TestCase):
    def test_stale_signal_policy_is_deterministic(self) -> None:
        self.assertEqual(stale_signal_policy(50.0, 1.0, 2.0), "fresh")
        self.assertEqual(stale_signal_policy(50.0, 2.0, 2.0), "fresh")
        self.assertEqual(stale_signal_policy(50.0, 2.5, 2.0), "stale_downgrade")
        self.assertEqual(stale_signal_policy(None, 1.0, 2.0), "stale_exclude")
        self.assertEqual(stale_signal_policy(50.0, None, 2.0), "stale_downgrade")
        self.assertEqual(
            stale_signal_policy(50.0, 5.0, 2.0, exclude_age_hours=4.0), "stale_exclude"
        )

    def test_stale_signal_policy_rejects_invalid_thresholds(self) -> None:
        with self.assertRaises(ValueError):
            stale_signal_policy(50.0, 1.0, -1.0)
        with self.assertRaises(ValueError):
            stale_signal_policy(50.0, 1.0, 2.0, exclude_age_hours=-1.0)


class FreshnessReportTests(unittest.TestCase):
    def test_report_is_json_serializable_and_deterministic(self) -> None:
        rows = []
        for i in range(6):
            rows.append(
                matured_row(
                    origin_at=_iso(5, 0, i),
                    horizon=2,
                    feature_name="derivatives_funding_rate_pct",
                    observation_at=_iso(5, 0, 0),
                    mae=0.4,
                    actual_change=5.0,
                )
            )
        for i in range(6):
            rows.append(
                matured_row(
                    origin_at=_iso(5, 1, i),
                    horizon=2,
                    feature_name="derivatives_funding_rate_pct",
                    observation_at=_iso(5, 0, 0),
                )
            )
        generated = "2026-01-06T00:00:00+00:00"
        first = build_freshness_report(rows, NOW, min_samples=5, generated_at=generated)
        second = build_freshness_report(rows, NOW, min_samples=5, generated_at=generated)
        self.assertEqual(first, second)
        payload = json.dumps(first, sort_keys=True)
        self.assertIn("derivatives_funding_rate_pct", payload)

        recs = first["recommendations"]
        self.assertTrue(recs)
        for rec in recs:
            self.assertEqual(rec["feature"], "derivatives_funding_rate_pct")
            self.assertIn(rec["action"], {"keep", "downgrade", "exclude"})
            self.assertIsInstance(rec["threshold_hours"], float)
            self.assertIsInstance(rec["evidence_samples"], int)
        two_hour = first["stale_thresholds"]["derivatives_funding_rate_pct"]["2h"]
        self.assertIn("threshold_hours", two_hour)
        self.assertEqual(first["policy"]["missing_feature_action"], "stale_exclude")
        self.assertEqual(first["leakage_safety"]["excluded_future_rows"], 0)
        self.assertIn("Feature freshness", render_markdown(first))

    def test_report_missing_feature_behavior_is_deterministic(self) -> None:
        row = matured_row(
            origin_at=_iso(5, 0, 0),
            horizon=4,
            feature_name="derivatives_funding_rate_pct",
            observation_at=_iso(5, 0, 0),
        )
        report = build_freshness_report([row], NOW, min_samples=1)
        self.assertEqual(report["samples"]["excluded_future_rows"], 0)

    def test_default_stale_thresholds_are_evidence_free_defaults(self) -> None:
        self.assertEqual(default_stale_threshold_hours("derivatives_funding_rate_pct"), 12.0)
        self.assertEqual(default_stale_threshold_hours("derivatives_open_interest_usd"), 2.5)
        self.assertEqual(default_stale_threshold_hours("microstructure_spread_bps"), 1.25)
        self.assertEqual(default_stale_threshold_hours("cross_eth_return_1h_pct"), 2.0)
        self.assertEqual(default_stale_threshold_hours("macro_vix_level"), 168.0)
        self.assertEqual(default_stale_threshold_hours("rsi_14"), 1.0)


if __name__ == "__main__":
    unittest.main()
