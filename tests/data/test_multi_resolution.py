#!/usr/bin/env python3
"""Tests for leakage-safe lower-timeframe market-context aggregates."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

import numpy as np

from btc_timesfm.data.multi_resolution import (
    AGGREGATION_RECIPE,
    MULTI_RESOLUTION_FEATURE_NAMES,
    derive_multi_resolution_features,
    feature_set_version,
    summarize_if_available,
)

FIVE_MIN = 300
REFERENCE_TS = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())


def synthetic_candles(
    hours: int,
    *,
    base: float = 60_000.0,
    seed: int = 0,
    end_ts: int = REFERENCE_TS,
) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    n = hours * 12
    timestamps = np.asarray([end_ts - (n - 1 - k) * FIVE_MIN for k in range(n)], dtype=np.int64)
    log_returns = rng.normal(0.0, 0.0008, size=n)
    log_prices = np.log(base) + np.cumsum(log_returns)
    closes = np.exp(log_prices)
    opens = np.empty_like(closes)
    opens[0] = closes[0] * (1.0 - 0.0002)
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


def flat_candles(hours: int, *, base: float = 60_000.0) -> dict[str, np.ndarray]:
    n = hours * 12
    timestamps = np.asarray(
        [REFERENCE_TS - (n - 1 - k) * FIVE_MIN for k in range(n)], dtype=np.int64
    )
    return {
        "timestamps": timestamps,
        "opens": np.full(n, base, dtype=np.float64),
        "highs": np.full(n, base + 1.0, dtype=np.float64),
        "lows": np.full(n, base - 1.0, dtype=np.float64),
        "closes": np.full(n, base, dtype=np.float64),
        "volumes": np.full(n, 100.0, dtype=np.float64),
    }


class MultiResolutionTests(unittest.TestCase):
    def test_alignment_uses_only_completed_timestamps(self) -> None:
        full = synthetic_candles(96, end_ts=REFERENCE_TS)
        origin_2 = REFERENCE_TS - 24 * 3600

        at_origin = derive_multi_resolution_features(
            full["timestamps"],
            full["opens"],
            full["highs"],
            full["lows"],
            full["closes"],
            full["volumes"],
            origin_ts=REFERENCE_TS,
        )
        truncated_index = np.flatnonzero(full["timestamps"] <= origin_2)
        truncated = {name: full[name][truncated_index] for name in full}
        at_cut = derive_multi_resolution_features(
            truncated["timestamps"],
            truncated["opens"],
            truncated["highs"],
            truncated["lows"],
            truncated["closes"],
            truncated["volumes"],
            origin_ts=origin_2,
        )
        future_included = derive_multi_resolution_features(
            full["timestamps"],
            full["opens"],
            full["highs"],
            full["lows"],
            full["closes"],
            full["volumes"],
            origin_ts=origin_2,
        )
        self.assertEqual(at_origin["aligned_candles"], 96 * 12)
        self.assertEqual(at_origin["excluded_candles_after_origin"], 0)
        self.assertEqual(future_included["aligned_candles"], 72 * 12)
        self.assertEqual(future_included["excluded_candles_after_origin"], 24 * 12)
        self.assertEqual(at_cut["aligned_candles"], 72 * 12)
        self.assertEqual(at_cut["excluded_candles_after_origin"], 0)
        self.assertEqual(future_included["features"], at_cut["features"])
        self.assertEqual(future_included["feature_set_version"], at_cut["feature_set_version"])

    def test_features_are_versioned_and_reproducible(self) -> None:
        candles = synthetic_candles(72)
        first = derive_multi_resolution_features(
            candles["timestamps"],
            candles["opens"],
            candles["highs"],
            candles["lows"],
            candles["closes"],
            candles["volumes"],
            origin_ts=REFERENCE_TS,
        )
        second = derive_multi_resolution_features(
            candles["timestamps"],
            candles["opens"],
            candles["highs"],
            candles["lows"],
            candles["closes"],
            candles["volumes"],
            origin_ts=REFERENCE_TS,
        )
        self.assertEqual(first["features"], second["features"])
        self.assertEqual(first["feature_set_version"], second["feature_set_version"])
        self.assertEqual(first["recipe_sha256"], second["recipe_sha256"])
        self.assertEqual(
            first["feature_set_version"], feature_set_version(recipe=AGGREGATION_RECIPE)
        )

        altered = dict(AGGREGATION_RECIPE)
        altered["momentum_1h_bars"] = 6
        self.assertNotEqual(feature_set_version(recipe=altered), feature_set_version())
        altered_result = derive_multi_resolution_features(
            candles["timestamps"],
            candles["opens"],
            candles["highs"],
            candles["lows"],
            candles["closes"],
            candles["volumes"],
            origin_ts=REFERENCE_TS,
            recipe=altered,
        )
        self.assertNotEqual(altered_result["features"], first["features"])
        self.assertNotEqual(altered_result["feature_set_version"], first["feature_set_version"])

    def test_summarize_if_available_degrades_without_data(self) -> None:
        missing = summarize_if_available(None, REFERENCE_TS)
        self.assertFalse(missing["available"])
        self.assertIsNone(missing["features"])
        self.assertEqual(missing["reason"], "no_data")

        partial = summarize_if_available({"timestamps": []}, REFERENCE_TS)
        self.assertFalse(partial["available"])
        self.assertEqual(partial["reason"], "missing_fields")

        empty = summarize_if_available({}, REFERENCE_TS)
        self.assertFalse(empty["available"])
        self.assertEqual(empty["reason"], "missing_fields")

    def test_present_data_is_available(self) -> None:
        candles = synthetic_candles(24)
        summary = summarize_if_available(candles, REFERENCE_TS)
        self.assertTrue(summary["available"])
        self.assertIsNotNone(summary["features"])
        self.assertTrue(summary["feature_set_version"].startswith("feature-set-"))

    def test_feature_set_is_bounded_and_finite(self) -> None:
        candles = synthetic_candles(96)
        result = derive_multi_resolution_features(
            candles["timestamps"],
            candles["opens"],
            candles["highs"],
            candles["lows"],
            candles["closes"],
            candles["volumes"],
            origin_ts=REFERENCE_TS,
        )
        features = result["features"]
        self.assertEqual(set(features), set(MULTI_RESOLUTION_FEATURE_NAMES))
        self.assertEqual(len(features), len(MULTI_RESOLUTION_FEATURE_NAMES))
        self.assertTrue(all(np.isfinite(float(value)) for value in features.values()))
        self.assertLessEqual(len(features), 24)

    def test_flat_candles_produce_neutral_finite_features(self) -> None:
        candles = flat_candles(96)
        result = derive_multi_resolution_features(
            candles["timestamps"],
            candles["opens"],
            candles["highs"],
            candles["lows"],
            candles["closes"],
            candles["volumes"],
            origin_ts=REFERENCE_TS,
        )
        features = result["features"]
        self.assertEqual(features["mr_momentum_1h_pct"], 0.0)
        self.assertEqual(features["mr_momentum_4h_pct"], 0.0)
        self.assertEqual(features["mr_vol_5m_1h_pct"], 0.0)
        self.assertEqual(features["mr_vol_15m_24h_pct"], 0.0)
        self.assertEqual(features["mr_volume_slope_1h"], 0.0)
        self.assertEqual(features["mr_close_position_1h_avg"], 0.5)
        self.assertEqual(features["mr_close_position_24h_avg"], 0.5)
        self.assertTrue(all(np.isfinite(float(value)) for value in features.values()))


if __name__ == "__main__":
    unittest.main()
