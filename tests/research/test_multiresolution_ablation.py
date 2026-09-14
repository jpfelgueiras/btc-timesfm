#!/usr/bin/env python3
"""Tests for the leakage-safe lower-timeframe walk-forward ablation."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

import numpy as np

from btc_timesfm.data.multi_resolution import (
    MULTI_RESOLUTION_FEATURE_NAMES,
    summarize_if_available,
)
from btc_timesfm.forecasting.forecast_engine import MarketData
from btc_timesfm.research.multiresolution_ablation import (
    build_feature_rows,
    eligible_training_rows,
    evaluate_multiresolution_ablation,
)

FIVE_MIN = 300
HOUR = 3600
REFERENCE_TS = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())


def synthetic_hourly(hours: int, *, base: float = 60_000.0, seed: int = 1) -> MarketData:
    rng = np.random.default_rng(seed)
    n = hours
    timestamps = [REFERENCE_TS - (n - 1 - k) * HOUR for k in range(n)]
    log_returns = rng.normal(0.0, 0.002, size=n)
    closes = base * np.exp(np.cumsum(log_returns))
    opens = np.empty_like(closes)
    opens[0] = closes[0] * 0.9999
    opens[1:] = closes[:-1]
    span = np.abs(rng.normal(0.0, 0.003, size=n))
    majors = np.maximum(opens, closes) * (1.0 + span)
    lows = np.minimum(opens, closes) * (1.0 - span)
    volumes = rng.uniform(0.5, 2.0, size=n) * 1000.0
    return MarketData(
        timestamps=timestamps,
        opens=opens.astype(np.float32),
        highs=majors.astype(np.float32),
        lows=lows.astype(np.float32),
        closes=closes.astype(np.float32),
        volumes=volumes.astype(np.float32),
    )


def synthetic_high_freq(
    hours: int, *, base: float = 60_000.0, seed: int = 2
) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    n = hours * 12
    timestamps = np.asarray(
        [REFERENCE_TS - (n - 1 - k) * FIVE_MIN for k in range(n)], dtype=np.int64
    )
    log_returns = rng.normal(0.0, 0.0008, size=n)
    closes = base * np.exp(np.cumsum(log_returns))
    opens = np.empty_like(closes)
    opens[0] = closes[0] * 0.9998
    opens[1:] = closes[:-1]
    span = np.abs(rng.normal(0.0, 0.0010, size=n))
    highs = np.maximum(opens, closes) * (1.0 + span)
    lows = np.minimum(opens, closes) * (1.0 - span)
    volumes = rng.uniform(0.5, 2.0, size=n) * 120.0
    return {
        "timestamps": timestamps,
        "opens": opens,
        "highs": highs,
        "lows": lows,
        "closes": closes,
        "volumes": volumes,
    }


def _build(hours: int = 640, samples: int = 48) -> list[dict]:
    data = synthetic_hourly(hours)
    high_freq = synthetic_high_freq(hours + 64)
    return build_feature_rows(data, high_freq, samples=samples)


class MultiResolutionAblationTests(unittest.TestCase):
    def test_report_schema_is_json_serializable(self) -> None:
        rows = _build()
        report = evaluate_multiresolution_ablation(rows, min_train=8)
        self.assertEqual(set(report["by_horizon"]), {"2h", "4h", "8h", "16h"})
        self.assertEqual(report["feature_names"], list(MULTI_RESOLUTION_FEATURE_NAMES))
        self.assertEqual(
            report["feature_sets"]["market_only"],
            report["feature_sets"]["market_plus_multiresolution"][
                : len(report["feature_sets"]["market_only"])
            ],
        )
        self.assertTrue(report["feature_set_version"].startswith("feature-set-"))
        self.assertFalse(report["uses_future_information"])
        self.assertIn("overall", report)
        json.dumps(report)

    def test_horizon_metrics_and_incremental_improvement(self) -> None:
        rows = _build()
        report = evaluate_multiresolution_ablation(rows, min_train=8)
        for item in report["by_horizon"].values():
            self.assertGreater(item["walk_forward_samples"], 0)
            self.assertIn("baseline_mae_pp", item)
            self.assertIn("candidate_mae_pp", item)
            self.assertIn("relative_mae_improvement", item)
            self.assertIn("significance", item)
        self.assertIn("mean_relative_mae_improvement", report["overall"])
        self.assertEqual(
            len(report["by_horizon"]["2h"]["origins"]),
            report["by_horizon"]["2h"]["walk_forward_samples"],
        )

    def test_training_rows_require_matured_target_and_multiresolution(self) -> None:
        rows = []
        for index in range(10):
            rows.append(
                {
                    "origin_at": f"2026-01-01T{index:02d}:00:00+00:00",
                    "origin_s": index * HOUR,
                    "base": [float(index % 3), 0.1, 0.2, 50.0, 0.1, 0.2, 0.3],
                    "multi": None
                    if index == 5
                    else [0.01 * index] * len(MULTI_RESOLUTION_FEATURE_NAMES),
                    "targets": {"2h": 0.0, "4h": 0.0, "8h": 0.0, "16h": 0.0},
                }
            )
        eligible = eligible_training_rows(rows, 7 * HOUR, 4)
        origins = [row["origin_s"] for row in eligible]
        self.assertNotIn(5 * HOUR, origins)
        self.assertEqual(origins, [0, HOUR, 2 * HOUR, 3 * HOUR])

    def test_graceful_degradation_without_lower_timeframe_data(self) -> None:
        data = synthetic_hourly(640)
        rows = build_feature_rows(data, None, samples=48)
        self.assertTrue(all(row["multi"] is None for row in rows))
        self.assertEqual(summarize_if_available(None, rows[0]["origin_s"])["available"], False)

        report = evaluate_multiresolution_ablation(rows, min_train=4)
        self.assertEqual(report["availability"]["rows_with_multiresolution"], 0)
        self.assertTrue(report["availability"]["degraded_to_hourly_only"])
        for item in report["by_horizon"].values():
            self.assertEqual(item["walk_forward_samples"], 0)
            self.assertIsNone(item["baseline_mae_pp"])
            self.assertIsNone(item["candidate_mae_pp"])

    def test_evaluation_is_deterministic(self) -> None:
        rows = _build()
        first = evaluate_multiresolution_ablation(rows, min_train=8)
        second = evaluate_multiresolution_ablation(rows, min_train=8)
        first.pop("runtime_estimate_seconds")
        second.pop("runtime_estimate_seconds")
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
