#!/usr/bin/env python3
"""Unit tests for the direction-first B1 X post formatter."""

from __future__ import annotations

import unittest

from btc_timesfm.x.format_tweet import (
    build_visual_tweet,
    consensus_text,
    direction_icon,
    direction_signal,
    horizon_text,
    previous_outcome_text,
)


def _score(predicted: float, actual: float, correct: bool) -> dict:
    return {
        "predicted_change_pct": predicted,
        "actual_change_pct": actual,
        "direction_correct": correct,
        "absolute_error_pct": abs(actual - predicted),
    }


def sample_output() -> dict:
    return {
        "latest_close_usd": 79_733.0,
        "regime": "range",
        "predictions": {
            "2h": {"price_usd": 79_813.0, "change_pct": 0.10, "model_agreement": 0.8},
            "4h": {"price_usd": 79_877.0, "change_pct": 0.18, "model_agreement": 0.8},
            "8h": {"price_usd": 80_004.0, "change_pct": 0.34, "model_agreement": 0.8},
            "16h": {"price_usd": 80_307.0, "change_pct": 0.72, "model_agreement": 0.8},
        },
        "forecast_confidence": {
            "status": "available",
            "label": "moderate",
            "score": 61.0,
            "minimum_evidence_samples": 28,
            "minimum_edge_vs_persistence_pct": 4.2,
        },
        "forecast_reliability": {
            "2h": _score(0.12, 0.07, True),
            "4h": _score(0.15, 0.22, True),
            "8h": _score(0.20, -0.11, False),
        },
    }


class VisualTweetTests(unittest.TestCase):
    def test_direction_icon_thresholds(self) -> None:
        self.assertEqual(direction_icon(0.006), "↗")
        self.assertEqual(direction_icon(-0.006), "↘")
        self.assertEqual(direction_icon(0.005), "→")
        self.assertEqual(direction_icon(-0.005), "→")

    def test_direction_signal_labels(self) -> None:
        self.assertEqual(direction_signal(0.10), ("🟢", "UP"))
        self.assertEqual(direction_signal(-0.10), ("🔴", "DOWN"))
        self.assertEqual(direction_signal(0.001), ("⚪", "FLAT"))

    def test_horizon_text_keeps_legacy_format(self) -> None:
        self.assertEqual(
            horizon_text("4h", {"price_usd": 80123.7, "change_pct": -0.42}),
            "4h ↘ $80,124 (-0.42%)",
        )

    def test_previous_outcome_includes_predicted_actual_and_delta(self) -> None:
        text = previous_outcome_text(_score(0.12, 0.07, True))
        self.assertEqual(text, "Prev ✅ P+0.12% A+0.07% Δ-0.05pp")

    def test_previous_outcome_handles_wrong_direction_and_positive_delta(self) -> None:
        text = previous_outcome_text(_score(-0.20, 0.11, False))
        self.assertEqual(text, "Prev ❌ P-0.20% A+0.11% Δ+0.31pp")

    def test_previous_outcome_missing_or_legacy_score_is_pending(self) -> None:
        self.assertEqual(previous_outcome_text(None), "Prev …")
        self.assertEqual(previous_outcome_text({"absolute_error_pct": 0.2}), "Prev …")

    def test_b1_tweet_contains_direction_accountability_and_confidence(self) -> None:
        tweet = build_visual_tweet(sample_output())
        self.assertLessEqual(len(tweet), 280)
        self.assertIn("₿ BTC SIGNAL", tweet)
        self.assertIn("2h 🟢 UP +0.10% | Prev ✅ P+0.12% A+0.07% Δ-0.05pp", tweet)
        self.assertIn("4h 🟢 UP +0.18% | Prev ✅ P+0.15% A+0.22% Δ+0.07pp", tweet)
        self.assertIn("8h 🟢 UP +0.34% | Prev ❌ P+0.20% A-0.11% Δ-0.31pp", tweet)
        self.assertIn("16h 🟢 UP +0.72% | Prev …", tweet)
        self.assertIn("📊", tweet)
        self.assertIn("edge", tweet)
        self.assertNotIn("🤝 4/4 bullish", tweet)
        self.assertIn("💰 $79,733 • ⚠️ Experimental • NFA", tweet)

    def test_bearish_neutral_and_mixed_current_signals(self) -> None:
        output = sample_output()
        output["predictions"]["2h"]["change_pct"] = -0.20
        output["predictions"]["4h"]["change_pct"] = -0.10
        output["predictions"]["8h"]["change_pct"] = 0.001
        output["predictions"]["16h"]["change_pct"] = 0.002

        tweet = build_visual_tweet(output)
        self.assertIn("2h 🔴 DOWN -0.20%", tweet)
        self.assertIn("8h ⚪ FLAT +0.00%", tweet)
        self.assertNotIn("mixed outlook", tweet)
        self.assertIn("📊", tweet)

    def test_insufficient_evidence_suppresses_confidence_claim(self) -> None:
        output = sample_output()
        output["forecast_confidence"] = {"status": "insufficient_evidence"}
        tweet = build_visual_tweet(output)
        self.assertNotIn("Confidence", tweet)
        self.assertNotIn("📊 Conf", tweet)

    def test_consensus_reports_dominant_direction_for_legacy_callers(self) -> None:
        predictions = sample_output()["predictions"]
        predictions["16h"]["change_pct"] = -0.1
        self.assertEqual(consensus_text(predictions), "🤝 3/4 bullish")

    def test_long_numbers_use_deterministic_compact_fallback(self) -> None:
        output = sample_output()
        for horizon in output["predictions"]:
            output["predictions"][horizon]["change_pct"] = 1.23456789e120
            output["forecast_reliability"][horizon] = _score(
                1.23456789e120,
                -9.87654321e119,
                False,
            )

        tweet = build_visual_tweet(output)
        self.assertLessEqual(len(tweet), 280)
        self.assertIn("P+1.2e+120%", tweet)
        self.assertIn("A-9.9e+119%", tweet)
        self.assertIn("Δ-2.2e+120pp", tweet)
        self.assertIn("⚠️ Experimental • NFA", tweet)


if __name__ == "__main__":
    unittest.main()
