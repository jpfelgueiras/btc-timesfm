#!/usr/bin/env python3
"""Tests for regime-specialized mixture-of-experts evaluation."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from btc_timesfm.data.regime_detection import validated_regime
from btc_timesfm.research.regime_specialists import (
    EXPERT_DEFINITIONS,
    evaluate_regime_specialists,
    expert_catalog,
    expert_model_weights,
)

MODEL_NAMES = ("timesfm_168h", "timesfm_336h", "ar1", "persistence")
HORIZONS = (2, 4, 8, 16)


def feature_row(index: int) -> dict:
    if index % 9 in {0, 1}:
        return {
            "volatility_6h_pct": 1.4,
            "volatility_24h_pct": 1.2,
            "volatility_7d_pct": 0.55,
            "range_24h_avg_pct": 1.7,
            "volume_zscore_7d": 2.0,
            "rsi_14": 56.0,
            "momentum_6h_pct": 0.4,
            "momentum_24h_pct": 0.7,
            "momentum_7d_pct": 1.0,
        }
    if index % 9 in {4, 5, 6}:
        return {
            "volatility_6h_pct": 0.25,
            "volatility_24h_pct": 0.30,
            "volatility_7d_pct": 0.35,
            "range_24h_avg_pct": 0.45,
            "volume_zscore_7d": 0.5,
            "rsi_14": 76.0,
            "momentum_6h_pct": 1.4,
            "momentum_24h_pct": 4.5,
            "momentum_7d_pct": 10.0,
        }
    return {
        "volatility_6h_pct": 0.30,
        "volatility_24h_pct": 0.35,
        "volatility_7d_pct": 0.42,
        "range_24h_avg_pct": 0.48,
        "volume_zscore_7d": 0.2,
        "rsi_14": 51.0,
        "momentum_6h_pct": 0.05,
        "momentum_24h_pct": 0.15,
        "momentum_7d_pct": 0.25,
    }


def make_sample(
    index: int,
    *,
    origin_timestamp: int,
    current: float = 100.0,
    actual: float = 100.15,
    empty_features: bool = False,
) -> dict:
    origin_at = datetime.fromtimestamp(origin_timestamp, tz=timezone.utc).isoformat()
    features = {} if empty_features else feature_row(index)
    model_predictions = {}
    for name, offset in (
        ("timesfm_168h", 0.25),
        ("timesfm_336h", 0.26),
        ("ar1", -0.10 if index % 2 else 0.10),
        ("persistence", 0.0),
    ):
        model_predictions[name] = {f"{hour}h": {"price_usd": current + offset} for hour in HORIZONS}
    return {
        "origin_timestamp": origin_timestamp,
        "current_price": current,
        "actuals": {f"{hour}h": actual for hour in HORIZONS},
        "forecast": {
            "latest_close_at": origin_at,
            "latest_close_usd": current,
            "regime": validated_regime(features) if features else "range",
            "market_features": features,
            "model_predictions": model_predictions,
            "predictions": {},
        },
    }


def build_samples(
    count: int,
    *,
    start: datetime | None = None,
    spacing_hours: int = 2,
    actual: float = 100.15,
    empty_features: bool = False,
) -> tuple[list[dict], dict[int, float]]:
    start = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    actuals: dict[int, float] = {}
    samples: list[dict] = []
    for index in range(count):
        origin_timestamp = int((start + timedelta(hours=index * spacing_hours)).timestamp())
        samples.append(
            make_sample(
                index,
                origin_timestamp=origin_timestamp,
                actual=actual,
                empty_features=empty_features,
            )
        )
        for hour in HORIZONS:
            actuals[origin_timestamp + hour * 3600] = actual
    return samples, actuals


class RegimeSpecialistsCatalogTests(unittest.TestCase):
    def test_expert_catalog_is_reproducible(self) -> None:
        first = expert_catalog()
        second = expert_catalog()
        self.assertEqual(first, second)
        self.assertIsNot(first, EXPERT_DEFINITIONS)
        self.assertIsNot(first["range"], EXPERT_DEFINITIONS["range"])
        self.assertEqual(set(first), {"range", "trending", "high_volatility", "fallback"})
        for config in first.values():
            for key in (
                "description",
                "history_limit",
                "confidence",
                "max_weight",
                "direction_reward",
                "persistence_fallback_boost",
                "active",
            ):
                self.assertIn(key, config)

    def test_regime_experts_are_distinct_configurations(self) -> None:
        catalog = expert_catalog()
        signatures = {
            tuple(
                catalog[name][key]
                for key in (
                    "history_limit",
                    "confidence",
                    "max_weight",
                    "direction_reward",
                    "persistence_fallback_boost",
                )
            )
            for name in ("range", "trending", "high_volatility")
        }
        self.assertEqual(len(signatures), 3)
        trending = catalog["trending"]
        self.assertGreater(trending["direction_reward"], catalog["range"]["direction_reward"])

    def test_expert_weights_are_valid_and_fallback_is_equal_ish(self) -> None:
        weights, diagnostics = expert_model_weights("fallback", list(MODEL_NAMES), None, 2, [], {})
        self.assertEqual(diagnostics["mode"], "fallback_static")
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=6)
        self.assertTrue(all(value > 0.0 for value in weights.values()))
        weights_range, diagnostics_range = expert_model_weights(
            "range", list(MODEL_NAMES), "range", 2, [], {}
        )
        self.assertEqual(diagnostics_range["mode"], "expert_range")
        self.assertAlmostEqual(sum(weights_range.values()), 1.0, places=6)
        with self.assertRaises(ValueError):
            expert_model_weights("bogus", list(MODEL_NAMES), None, 2, [], {})


class RegimeSpecialistsEvaluationTests(unittest.TestCase):
    def test_report_compares_baseline_and_candidate_by_horizon_and_regime(self) -> None:
        samples, actuals = build_samples(24)
        report = evaluate_regime_specialists(samples, actuals)
        self.assertEqual(report["samples"], 24)
        self.assertEqual(set(report["by_horizon"]), {"2h", "4h", "8h", "16h"})
        for horizon in report["by_horizon"].values():
            self.assertEqual(horizon["samples"], 24)
            self.assertEqual(horizon["baseline"]["samples"], 24)
            self.assertEqual(horizon["candidate"]["samples"], 24)
            self.assertIsNotNone(horizon["baseline"]["mae_pct"])
            self.assertIsNotNone(horizon["candidate"]["mae_pct"])
        self.assertEqual(
            set(report["by_regime"]), {"range", "trending", "high_volatility", "unknown"}
        )
        for table in report["by_regime"].values():
            self.assertEqual(set(table), {"2h", "4h", "8h", "16h"})

    def test_unobserved_future_actuals_cannot_affect_earlier_weighting(self) -> None:
        samples, actuals = build_samples(
            4, start=datetime(2026, 1, 1, tzinfo=timezone.utc), spacing_hours=1
        )
        first = evaluate_regime_specialists(samples, actuals)
        future_actuals = dict(actuals)
        future_actuals[
            int((datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=30)).timestamp())
        ] = 100000.0
        second = evaluate_regime_specialists(samples, future_actuals)
        self.assertEqual(first, second)

    def test_transition_churn_is_measured_from_expert_sequence(self) -> None:
        samples, actuals = build_samples(24)
        report = evaluate_regime_specialists(samples, actuals)
        sequence = report["chosen_expert_sequence"]
        self.assertEqual(len(sequence), 24)
        expected_transitions = sum(left != right for left, right in zip(sequence, sequence[1:]))
        churn = report["transition_churn"]
        self.assertEqual(churn["samples"], 24)
        self.assertEqual(churn["transitions"], expected_transitions)
        expected_rate = expected_transitions / (len(sequence) - 1)
        self.assertAlmostEqual(churn["transition_rate"], expected_rate, places=6)

    def test_fallback_expert_used_for_unknown_regime(self) -> None:
        samples, actuals = build_samples(1, empty_features=True)
        report = evaluate_regime_specialists(samples, actuals)
        self.assertEqual(report["chosen_expert_sequence"], ["fallback"])
        self.assertEqual(report["fallback_reason_sequence"], ["unknown_regime"])
        self.assertEqual(report["fallback_usage"]["occurrences"], 1)
        self.assertEqual(report["fallback_usage"]["reasons"]["unknown_regime"], 1)
        self.assertEqual(report["fallback_usage"]["reasons"]["sparse_history"], 0)
        self.assertEqual(report["expert_utilization"]["fallback"], 1)

    def test_fallback_expert_used_for_sparse_history(self) -> None:
        samples, actuals = build_samples(3, spacing_hours=1)
        report = evaluate_regime_specialists(samples, actuals)
        self.assertEqual(report["chosen_expert_sequence"], ["fallback", "fallback", "fallback"])
        self.assertEqual(
            report["fallback_reason_sequence"],
            ["sparse_history", "sparse_history", "sparse_history"],
        )
        self.assertEqual(report["fallback_usage"]["reasons"]["sparse_history"], 3)
        self.assertEqual(report["fallback_usage"]["reasons"]["unknown_regime"], 0)

    def test_statistical_comparison_output_shape(self) -> None:
        samples, actuals = build_samples(24)
        report = evaluate_regime_specialists(samples, actuals)
        statistical = report["by_horizon"]["2h"]["statistical"]
        for metric in ("mae_pct", "direction_accuracy"):
            entry = statistical[metric]
            for key in (
                "metric",
                "samples",
                "candidate_mean",
                "baseline_mean",
                "mean_improvement",
                "improvement_ci",
                "probability_candidate_better",
                "conclusion",
                "reason",
                "low_sample",
            ):
                self.assertIn(key, entry)
            self.assertIn("lower", entry["improvement_ci"])
            self.assertIn("upper", entry["improvement_ci"])

    def test_low_sample_segments_are_marked_inconclusive(self) -> None:
        samples, actuals = build_samples(24)
        report = evaluate_regime_specialists(samples, actuals)
        self.assertTrue(report["by_horizon"]["2h"]["low_sample"])
        self.assertTrue(report["by_horizon"]["2h"]["inconclusive"])
        self.assertGreater(len(report["inconclusive_segments"]), 0)
        for segment in report["inconclusive_segments"]:
            self.assertIn("table", segment)
            self.assertIn("horizon", segment)


if __name__ == "__main__":
    unittest.main()
