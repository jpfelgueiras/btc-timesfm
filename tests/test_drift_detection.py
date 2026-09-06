#!/usr/bin/env python3
"""Synthetic tests for leakage-safe production drift detection."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from btc_timesfm.ops.drift_detection import (
    DriftConfig,
    adaptive_confidence_for_severity,
    evaluate_drift,
    persist_drift_report,
)


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def error_rows(
    *,
    baseline_errors: list[float],
    recent_errors: list[float],
    baseline_direction: list[int] | None = None,
    recent_direction: list[int] | None = None,
    model: str = "timesfm_168h",
    horizon: int = 2,
) -> list[dict[str, object]]:
    errors = [*baseline_errors, *recent_errors]
    directions = [
        *(baseline_direction or [1] * len(baseline_errors)),
        *(recent_direction or [1] * len(recent_errors)),
    ]
    rows: list[dict[str, object]] = []
    for index, (error, direction) in enumerate(zip(errors, directions, strict=True)):
        target = START + timedelta(hours=index * 2)
        rows.append(
            {
                "origin_at": (target - timedelta(hours=horizon)).isoformat(),
                "target_at": target.isoformat(),
                "model_name": model,
                "horizon_hours": horizon,
                "absolute_error_pct": error,
                "signed_error_pct": error if index % 2 == 0 else -error,
                "direction_correct": direction,
                "matured_at": (target + timedelta(minutes=5)).isoformat(),
            }
        )
    return rows


def feature_rows(values: list[float]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index, value in enumerate(values):
        rows.append(
            {
                "origin_at": (START + timedelta(hours=index * 2)).isoformat(),
                "market_features": {
                    "volatility_24h_pct": value,
                    "range_24h_avg_pct": value * 1.2,
                    "volume_zscore_7d": value - 1.0,
                    "rsi_14": 50.0 + value,
                    "momentum_24h_pct": value * 0.5,
                },
            }
        )
    return rows


class DriftDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = DriftConfig(
            error_recent=8,
            error_baseline=24,
            feature_recent=12,
            feature_baseline=36,
            warning_shift_z=2.0,
            severe_shift_z=3.5,
            warning_ks=0.35,
            severe_ks=0.55,
            warning_direction_drop=0.15,
            severe_direction_drop=0.25,
            warning_adaptive_confidence=0.5,
            severe_adaptive_confidence=0.0,
        )
        self.evaluated_at = datetime(2026, 2, 1, tzinfo=timezone.utc)

    def test_stable_distributions_do_not_raise_drift(self) -> None:
        error_pattern = [0.4, 0.5, 0.6, 0.5, 0.4, 0.6, 0.5, 0.5]
        errors = error_rows(
            baseline_errors=error_pattern * 3,
            recent_errors=error_pattern,
        )
        feature_pattern = [0.8, 0.9, 1.0, 1.1, 1.0, 0.9, 0.8, 1.0, 1.1, 0.9, 1.0, 1.0]
        features = feature_rows(feature_pattern * 4)

        report = evaluate_drift(
            errors,
            features,
            config=self.config,
            evaluated_at=self.evaluated_at,
        )

        self.assertEqual(report["severity"], "none")
        self.assertEqual(report["adaptive_confidence"], 1.0)
        self.assertEqual(report["summary"]["events"], 0)
        self.assertGreater(report["summary"]["signals_evaluated"], 0)

    def test_large_matured_error_shift_is_severe(self) -> None:
        baseline = [0.45, 0.50, 0.55, 0.50, 0.45, 0.55, 0.50, 0.50] * 3
        recent = [2.8, 3.0, 3.2, 3.1, 2.9, 3.3, 3.0, 3.2]
        errors = error_rows(
            baseline_errors=baseline,
            recent_errors=recent,
            baseline_direction=[1, 1, 1, 0, 1, 1, 0, 1] * 3,
            recent_direction=[0] * 8,
        )
        # An unmatured prediction has no error fields and must never affect drift.
        errors.append(
            {
                "origin_at": "2099-01-01T00:00:00+00:00",
                "target_at": "2099-01-01T02:00:00+00:00",
                "model_name": "timesfm_168h",
                "horizon_hours": 2,
                "absolute_error_pct": None,
                "signed_error_pct": None,
                "direction_correct": None,
            }
        )

        report = evaluate_drift(
            errors,
            [],
            config=self.config,
            evaluated_at=self.evaluated_at,
        )

        self.assertEqual(report["severity"], "severe")
        self.assertEqual(report["adaptive_confidence"], 0.0)
        self.assertEqual(report["fallback_mode"], "static_prior")
        event = report["events"][0]
        self.assertEqual(event["signal_key"], "error:timesfm_168h:2h")
        self.assertEqual(event["severity"], "severe")
        self.assertGreater(event["metrics"]["direction_accuracy_drop"], 0.25)

    def test_feature_shift_can_include_current_completed_candle(self) -> None:
        baseline_pattern = [0.9, 1.0, 1.1, 1.0, 0.95, 1.05] * 6
        shifted = [4.0] * 11
        rows = feature_rows([*baseline_pattern, *shifted])
        current_origin = (START + timedelta(hours=len(rows) * 2)).isoformat()
        current = {
            "volatility_24h_pct": 4.0,
            "range_24h_avg_pct": 4.8,
            "volume_zscore_7d": 3.0,
            "rsi_14": 54.0,
            "momentum_24h_pct": 2.0,
        }

        report = evaluate_drift(
            [],
            rows,
            current_features=current,
            current_origin_at=current_origin,
            config=self.config,
            evaluated_at=self.evaluated_at,
        )

        self.assertEqual(report["severity"], "severe")
        self.assertEqual(report["latest_observed_origin_at"], current_origin)
        self.assertIn(
            "feature:volatility_24h_pct", {event["signal_key"] for event in report["events"]}
        )

    def test_thresholds_and_confidence_are_reproducible(self) -> None:
        self.assertEqual(adaptive_confidence_for_severity("none", self.config), 1.0)
        self.assertEqual(adaptive_confidence_for_severity("warning", self.config), 0.5)
        self.assertEqual(adaptive_confidence_for_severity("severe", self.config), 0.0)

        pattern = [0.5] * 32
        first = evaluate_drift(
            error_rows(baseline_errors=pattern[:24], recent_errors=pattern[24:]),
            [],
            config=self.config,
            evaluated_at=self.evaluated_at,
        )
        second = evaluate_drift(
            error_rows(baseline_errors=pattern[:24], recent_errors=pattern[24:]),
            [],
            config=self.config,
            evaluated_at=self.evaluated_at,
        )
        self.assertEqual(first, second)
        self.assertEqual(first["configuration"]["severe_shift_z"], 3.5)

    def test_report_and_actions_summary_are_written(self) -> None:
        report = evaluate_drift(
            [],
            [],
            config=self.config,
            evaluated_at=self.evaluated_at,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "drift.json"
            summary_path = root / "drift.md"
            persist_drift_report(
                report,
                report_path=report_path,
                summary_path=summary_path,
            )
            self.assertTrue(report_path.exists())
            summary = summary_path.read_text(encoding="utf-8")
            self.assertIn("Production drift detection", summary)
            self.assertIn("Overall state: **NONE**", summary)


if __name__ == "__main__":
    unittest.main()
