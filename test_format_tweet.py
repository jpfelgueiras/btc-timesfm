#!/usr/bin/env python3
"""Unit tests for the emoji-rich X post formatter."""

from __future__ import annotations

import unittest

from format_tweet import build_visual_tweet, direction_icon, horizon_text


def sample_output(*, regime: str = "range") -> dict:
    return {
        "latest_close_usd": 79_733.0,
        "regime": regime,
        "predictions": {
            "2h": {"price_usd": 79_735.0, "change_pct": 0.003, "model_agreement": 0.4},
            "4h": {"price_usd": 79_739.0, "change_pct": 0.008, "model_agreement": 0.4},
            "8h": {"price_usd": 79_751.0, "change_pct": 0.023, "model_agreement": 0.8},
            "16h": {"price_usd": 79_780.0, "change_pct": 0.060, "model_agreement": 0.8},
        },
        "forecast_reliability": {
            "2h": {"absolute_error_pct": 0.136},
            "4h": {"absolute_error_pct": 0.135},
        },
    }


class VisualTweetTests(unittest.TestCase):
    def test_direction_icon_thresholds(self) -> None:
        self.assertEqual(direction_icon(0.006), "↗")
        self.assertEqual(direction_icon(-0.006), "↘")
        self.assertEqual(direction_icon(0.005), "→")
        self.assertEqual(direction_icon(-0.005), "→")

    def test_horizon_text_formats_price_and_change(self) -> None:
        self.assertEqual(
            horizon_text("4h", {"price_usd": 80123.7, "change_pct": -0.42}),
            "4h ↘ $80,124 (-0.42%)",
        )

    def test_visual_tweet_contains_key_signals_and_stays_within_limit(self) -> None:
        tweet = build_visual_tweet(sample_output())
        self.assertLessEqual(len(tweet), 280)
        self.assertIn("₿ BTC/USD • Ensemble", tweet)
        self.assertIn("↔️ Range", tweet)
        self.assertIn("🤝 Models 60% agree", tweet)
        self.assertIn("🎯 Error: 2h 0.14% | 4h 0.14% | 8h — | 16h —", tweet)
        self.assertIn("⚠️ Experimental • NFA", tweet)

    def test_unknown_regime_gets_readable_fallback_label(self) -> None:
        tweet = build_visual_tweet(sample_output(regime="breakout_watch"))
        self.assertIn("🧭 Breakout Watch", tweet)

    def test_long_regime_drops_agreement_before_exceeding_x_limit(self) -> None:
        tweet = build_visual_tweet(sample_output(regime="x" * 60))
        self.assertLessEqual(len(tweet), 280)
        self.assertNotIn("🤝 Models", tweet)

    def test_extreme_regime_length_raises_instead_of_posting_oversize_text(self) -> None:
        with self.assertRaises(RuntimeError):
            build_visual_tweet(sample_output(regime="x" * 400))


if __name__ == "__main__":
    unittest.main()
