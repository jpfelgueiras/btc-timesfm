#!/usr/bin/env python3
"""Tests for no-lookahead regime detector evaluation."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from btc_timesfm.research.regime_backtest import compare_detectors_out_of_sample


def feature_row(i: int) -> dict:
    if i % 9 in {0, 1}:
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
    if i % 9 in {4, 5, 6}:
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


class RegimeBacktestTests(unittest.TestCase):
    def test_report_contains_churn_regime_metrics_and_safety_gate(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        samples = []
        actuals: dict[int, float] = {}
        for i in range(24):
            origin = start + timedelta(hours=i * 2)
            current = 100.0 + i * 0.1
            model_predictions = {}
            for name, offset in (
                ("timesfm_168h", 0.25),
                ("timesfm_336h", 0.26),
                ("ar1", -0.10 if i % 2 else 0.10),
                ("persistence", 0.0),
            ):
                model_predictions[name] = {
                    f"{hour}h": {"price_usd": current + offset} for hour in (2, 4, 8, 16)
                }
            sample_actuals = {}
            for hour in (2, 4, 8, 16):
                target = int((origin + timedelta(hours=hour)).timestamp())
                value = current + 0.15
                actuals[target] = value
                sample_actuals[f"{hour}h"] = value
            samples.append(
                {
                    "origin_timestamp": int(origin.timestamp()),
                    "current_price": current,
                    "actuals": sample_actuals,
                    "forecast": {
                        "latest_close_at": origin.isoformat(),
                        "latest_close_usd": current,
                        "regime": "range",
                        "market_features": feature_row(i),
                        "model_predictions": model_predictions,
                        "predictions": {},
                    },
                }
            )

        report = compare_detectors_out_of_sample(samples, actuals)
        self.assertEqual(report["samples"], 24)
        self.assertEqual(set(report["by_horizon"]), {"2h", "4h", "8h", "16h"})
        self.assertEqual(set(report["transition_churn"]), {"heuristic", "validated"})
        self.assertIn("validated_score", report["detector_methods"])
        self.assertIn("passes", report["safety_gate"])
        for horizon in report["by_horizon"].values():
            self.assertEqual(horizon["heuristic"]["samples"], 24)
            self.assertEqual(horizon["validated"]["samples"], 24)

    def test_unobserved_future_actuals_cannot_affect_earlier_weighting(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        model_predictions = {
            name: {f"{hour}h": {"price_usd": 100.1} for hour in (2, 4, 8, 16)}
            for name in ("timesfm_168h", "timesfm_336h", "ar1", "persistence")
        }
        samples = []
        actuals: dict[int, float] = {}
        for i in range(4):
            origin = start + timedelta(hours=i)
            sample_actuals = {}
            for hour in (2, 4, 8, 16):
                target = int((origin + timedelta(hours=hour)).timestamp())
                actuals[target] = 100.2
                sample_actuals[f"{hour}h"] = 100.2
            samples.append(
                {
                    "origin_timestamp": int(origin.timestamp()),
                    "current_price": 100.0,
                    "actuals": sample_actuals,
                    "forecast": {
                        "latest_close_at": origin.isoformat(),
                        "latest_close_usd": 100.0,
                        "market_features": feature_row(i),
                        "model_predictions": model_predictions,
                        "predictions": {},
                    },
                }
            )
        first = compare_detectors_out_of_sample(samples, actuals)
        future_actuals = dict(actuals)
        future_actuals[int((start + timedelta(days=30)).timestamp())] = 100000.0
        second = compare_detectors_out_of_sample(samples, future_actuals)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
