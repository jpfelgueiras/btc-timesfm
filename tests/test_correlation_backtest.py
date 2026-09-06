#!/usr/bin/env python3
"""Tests for the frozen-sample correlation weighting backtest."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from btc_timesfm.research.correlation_backtest import compare_frozen_samples


class CorrelationBacktestTests(unittest.TestCase):
    def test_report_compares_policies_on_same_origins_without_future_actuals(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        samples = []
        actuals: dict[int, float] = {}
        for i in range(24):
            origin = start + timedelta(hours=i * 2)
            current = 100.0 + i * 0.1
            model_predictions = {}
            for name, offset in (
                ("timesfm_168h", 0.3),
                ("timesfm_336h", 0.31),
                ("ar1", -0.2 if i % 2 else 0.2),
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
                        "model_predictions": model_predictions,
                        "predictions": {},
                    },
                }
            )

        report = compare_frozen_samples(samples, actuals)
        self.assertEqual(report["samples"], 24)
        self.assertEqual(set(report["by_horizon"]), {"2h", "4h", "8h", "16h"})
        for metrics in report["by_horizon"].values():
            self.assertEqual(metrics["current_adaptive"]["samples"], 24)
            self.assertEqual(metrics["correlation_aware"]["samples"], 24)
            self.assertIn("correlation_minus_current_mae_pct", metrics)

    def test_order_of_input_samples_does_not_change_report(self) -> None:
        origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
        model_predictions = {
            name: {f"{hour}h": {"price_usd": 100.0} for hour in (2, 4, 8, 16)}
            for name in ("timesfm_168h", "timesfm_336h", "ar1", "persistence")
        }
        samples = []
        actuals: dict[int, float] = {}
        for i in range(3):
            at = origin + timedelta(hours=i)
            sample_actuals = {}
            for hour in (2, 4, 8, 16):
                target = int((at + timedelta(hours=hour)).timestamp())
                actuals[target] = 100.0
                sample_actuals[f"{hour}h"] = 100.0
            samples.append(
                {
                    "origin_timestamp": int(at.timestamp()),
                    "current_price": 100.0,
                    "actuals": sample_actuals,
                    "forecast": {
                        "latest_close_at": at.isoformat(),
                        "latest_close_usd": 100.0,
                        "regime": "range",
                        "model_predictions": model_predictions,
                        "predictions": {},
                    },
                }
            )
        self.assertEqual(
            compare_frozen_samples(samples, actuals),
            compare_frozen_samples(list(reversed(samples)), actuals),
        )


if __name__ == "__main__":
    unittest.main()
