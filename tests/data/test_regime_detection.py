#!/usr/bin/env python3
"""Tests for validated market regime detection."""

from __future__ import annotations

import unittest

from btc_timesfm.data.regime_detection import (
    compare_regime_methods,
    heuristic_regime,
    prototype_regime,
    regime_scores,
    smooth_regime_sequence,
    transition_churn,
    validated_regime,
)


def features(
    *,
    vol6: float = 0.35,
    vol24: float = 0.40,
    vol7d: float = 0.45,
    mom6: float = 0.10,
    mom24: float = 0.20,
    mom7d: float = 0.30,
    rsi: float = 52.0,
    volume_z: float = 0.2,
    range24: float = 0.5,
) -> dict:
    return {
        "volatility_6h_pct": vol6,
        "volatility_24h_pct": vol24,
        "volatility_7d_pct": vol7d,
        "momentum_6h_pct": mom6,
        "momentum_24h_pct": mom24,
        "momentum_7d_pct": mom7d,
        "rsi_14": rsi,
        "volume_zscore_7d": volume_z,
        "range_24h_avg_pct": range24,
    }


class RegimeDetectionTests(unittest.TestCase):
    def test_legacy_heuristic_is_preserved_as_explicit_benchmark(self) -> None:
        self.assertEqual(heuristic_regime(features(vol24=1.5, vol7d=0.8)), "high_volatility")
        self.assertEqual(heuristic_regime(features(mom24=3.0, rsi=72.0)), "trending")
        self.assertEqual(heuristic_regime(features()), "range")

    def test_validated_detector_covers_all_stable_labels(self) -> None:
        self.assertEqual(validated_regime(features()), "range")
        self.assertEqual(
            validated_regime(features(vol6=1.8, vol24=1.5, vol7d=0.65, range24=2.0, volume_z=2.5)),
            "high_volatility",
        )
        self.assertEqual(
            validated_regime(
                features(
                    vol6=0.25,
                    vol24=0.30,
                    vol7d=0.35,
                    mom6=1.5,
                    mom24=5.0,
                    mom7d=12.0,
                    rsi=78.0,
                )
            ),
            "trending",
        )

    def test_scores_are_reproducible_and_non_negative(self) -> None:
        item = features(mom24=-2.0, volume_z=-2.2)
        self.assertEqual(regime_scores(item), regime_scores(dict(item)))
        for score in regime_scores(item).values():
            self.assertGreaterEqual(score, 0.0)

    def test_fixed_prototype_alternative_is_deterministic(self) -> None:
        item = features(vol24=0.8, vol7d=0.5, mom24=1.2, rsi=68.0)
        self.assertIn(prototype_regime(item), {"range", "trending", "high_volatility"})
        self.assertEqual(prototype_regime(item), prototype_regime(item))

    def test_transition_confirmation_reduces_single_sample_churn(self) -> None:
        rows = [
            features(),
            features(vol6=2.0, vol24=1.8, vol7d=0.7, range24=2.0),
            features(),
            features(),
        ]
        raw = [validated_regime(item) for item in rows]
        confirmed = smooth_regime_sequence(rows, confirmation_samples=2)
        self.assertLessEqual(
            transition_churn(confirmed)["transitions"], transition_churn(raw)["transitions"]
        )

    def test_method_comparison_reports_churn_and_label_counts(self) -> None:
        report = compare_regime_methods([features(), features(mom24=4.0, rsi=75.0)])
        self.assertEqual(set(report), {"heuristic", "validated_score", "fixed_prototype"})
        for metrics in report.values():
            self.assertIn("raw_churn", metrics)
            self.assertIn("confirmed_churn", metrics)
            self.assertEqual(metrics["raw_churn"]["samples"], 2)

    def test_missing_or_invalid_optional_values_fail_conservatively(self) -> None:
        item = features()
        item["volume_zscore_7d"] = "not-a-number"
        del item["momentum_7d_pct"]
        self.assertIn(validated_regime(item), {"range", "trending", "high_volatility"})


if __name__ == "__main__":
    unittest.main()
