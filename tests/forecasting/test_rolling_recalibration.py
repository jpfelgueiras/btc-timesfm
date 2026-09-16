#!/usr/bin/env python3
"""Tests for rolling recalibration (#159)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from btc_timesfm.forecasting.rolling_recalibration import recalibrate

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def snapshot(
    origin: datetime,
    *,
    actual: float | None,
    regime: str = "range",
    volatility_6h_pct: float = 0.5,
    point: float = 100.0,
    half_width: float = 10.0,
) -> dict:
    horizons = {}
    outcomes = {}
    for hour in (2, 4, 8, 16):
        horizons[f"{hour}h"] = {
            "price_usd": point,
            "q10_usd": point - half_width,
            "q90_usd": point + half_width,
        }
        if actual is not None:
            outcomes[f"{hour}h"] = {"ensemble": {"actual_target_price_usd": actual}}
    return {
        "latest_close_at": origin.isoformat(),
        "regime": regime,
        "market_features": {"volatility_6h_pct": volatility_6h_pct},
        "predictions": horizons,
        "_outcomes": outcomes,
    }


class RollingRecalibrationTests(unittest.TestCase):
    def test_recalibration_detects_breach(self) -> None:
        # Create history where coverage is poor
        history = [
            snapshot(
                START + timedelta(hours=i),
                actual=150.0,  # Will be outside [90, 110]
                point=100.0,
                half_width=10.0,
            )
            for i in range(30)
        ]

        current_multipliers = {"2h": 1.0, "4h": 1.0, "8h": 1.0, "16h": 1.0}

        report = recalibrate(
            history,
            {},
            current_multipliers,
            min_samples=20,
        )

        self.assertEqual(report["action"], "recalibrate")
        self.assertTrue(report["horizons"]["2h"]["is_breach"])
        self.assertIn("2h", report["changes"])

    def test_recalibration_enforces_guardrails(self) -> None:
        # Extreme breach
        history = [
            snapshot(
                START + timedelta(hours=i),
                actual=200.0,
                point=100.0,
                half_width=1.0,  # Very narrow
            )
            for i in range(30)
        ]

        current_multipliers = {"2h": 1.0}

        report = recalibrate(
            history,
            {},
            current_multipliers,
            min_samples=20,
        )

        # New multiplier should be capped at 1.0 + 0.10 = 1.10
        self.assertLessEqual(report["new_multipliers"]["2h"], 1.10)
        self.assertGreaterEqual(report["new_multipliers"]["2h"], 0.90)


if __name__ == "__main__":
    unittest.main()
