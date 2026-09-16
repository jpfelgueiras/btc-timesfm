#!/usr/bin/env python3
"""Tests for safe rolling recalibration (#159)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from btc_timesfm.forecasting.rolling_recalibration import (
    MAX_MULTIPLIER_ADJUSTMENT,
    append_audit_entry,
    recalibrate,
    rollback_last_recalibration,
)

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def snapshot(
    origin: datetime, *, actual: float | None, point: float = 100.0, half_width: float = 10.0
) -> dict:
    predictions = {
        f"{hour}h": {
            "price_usd": point,
            "q10_usd": point - half_width,
            "q90_usd": point + half_width,
        }
        for hour in (2, 4, 8, 16)
    }
    outcomes = (
        {f"{hour}h": {"ensemble": {"actual_target_price_usd": actual}} for hour in (2, 4, 8, 16)}
        if actual is not None
        else {}
    )
    return {
        "latest_close_at": origin.isoformat(),
        "latest_close_usd": 100.0,
        "regime": "range",
        "market_features": {"volatility_6h_pct": 0.5},
        "predictions": predictions,
        "_outcomes": outcomes,
    }


class RollingRecalibrationTests(unittest.TestCase):
    def history(self, actual: float = 150.0, point: float = 100.0) -> list[dict]:
        return [snapshot(START + timedelta(hours=i), actual=actual, point=point) for i in range(30)]

    def test_uses_only_matured_history_cutoff(self) -> None:
        now = START + timedelta(hours=31)
        report = recalibrate(
            self.history() + [snapshot(now, actual=150.0)], {}, {"2h": 1.0}, min_samples=20, now=now
        )
        self.assertEqual(report["horizons"]["2h"]["matured_samples"], 30)

    def test_detects_coverage_and_directional_breach(self) -> None:
        report = recalibrate(
            self.history(point=90.0), {}, {"2h": 1.0}, min_samples=20, now=START + timedelta(days=3)
        )
        horizon = report["horizons"]["2h"]
        self.assertTrue(horizon["coverage_breach"])
        self.assertTrue(horizon["directional_breach"])
        self.assertIn(
            {"horizon": "2h", "reason": "directional_calibration_drift"}, report["alerts"]
        )

    def test_bounds_safe_adjustment(self) -> None:
        report = recalibrate(
            self.history(actual=112.0),
            {},
            {"2h": 1.0},
            min_samples=20,
            now=START + timedelta(days=3),
        )

        change = report["changes"]["2h"]
        self.assertEqual(change["status"], "applied")
        self.assertLessEqual(change["after"], 1.0 + MAX_MULTIPLIER_ADJUSTMENT)
        self.assertTrue(change["bounded"])

    def test_rejects_unsafe_adjustment_and_alerts(self) -> None:
        report = recalibrate(
            self.history(), {}, {"2h": 0.5}, min_samples=20, now=START + timedelta(days=3)
        )
        change = report["changes"]["2h"]
        self.assertEqual(report["action"], "alert")
        self.assertEqual(change["status"], "rejected")
        self.assertEqual(change["reason"], "unsafe_multiplier_change")
        self.assertIn({"horizon": "2h", "reason": "unsafe_multiplier_change"}, report["alerts"])

    def test_audit_history_and_rollback(self) -> None:
        audit: list[dict] = []
        report = recalibrate(
            self.history(actual=111.0),
            {},
            {"2h": 1.0},
            min_samples=20,
            now=START + timedelta(days=3),
            audit_history=audit,
        )

        self.assertEqual(audit[0], report["audit_entry"])
        restored = rollback_last_recalibration(
            report["new_multipliers"], audit, now=START + timedelta(days=4)
        )
        self.assertEqual(restored["action"], "rollback")
        self.assertEqual(restored["new_multipliers"]["2h"], 1.0)
        self.assertEqual(audit[-1]["action"], "rollback")

    def test_append_audit_entry(self) -> None:
        audit: list[dict] = []
        append_audit_entry(
            audit,
            {"generated_at": START.isoformat(), "action": "none", "changes": {}, "alerts": []},
        )
        self.assertEqual(audit[0]["action"], "none")


if __name__ == "__main__":
    unittest.main()
