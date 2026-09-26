"""Tests for the bounded incremental feature evidence gate."""

from __future__ import annotations

import unittest

from btc_timesfm.forecasting.feature_registry import FEATURE_REGISTRY, MARKET_FEATURE_NAMES
from btc_timesfm.research.incremental_feature_value import (
    CANDIDATE_GROUPS,
    audit_feature_rows,
    baseline_or_candidate,
)


class IncrementalFeatureValueTests(unittest.TestCase):
    def test_as_of_requires_capture_and_vintage_at_or_before_origin(self) -> None:
        report = audit_feature_rows(
            [
                {
                    "origin_at": "2025-01-01T01:00:00+00:00",
                    "features": {"volume_zscore_7d": 1.0},
                    "feature_capture_times": {"volume_zscore_7d": "2025-01-01T00:00:00Z"},
                    "feature_vintages": {"volume_zscore_7d": "2025-01-01T01:00:00Z"},
                },
                {
                    "origin_at": "2025-01-01T01:00:00+00:00",
                    "features": {"volume_zscore_7d": 2.0},
                    "feature_capture_times": {"volume_zscore_7d": "2025-01-01T01:01:00Z"},
                    "feature_vintages": {"volume_zscore_7d": "2025-01-01T00:00:00Z"},
                },
            ]
        )
        volume = report["groups"]["volume_transform"]
        self.assertEqual(volume["all_origin_count"], 2)
        self.assertEqual(volume["available_by_feature"]["volume_zscore_7d"], 1)
        self.assertEqual(volume["common_available_origin_indices"], [0])

    def test_common_cohort_and_missingness_use_all_origins_as_denominator(self) -> None:
        group_features = CANDIDATE_GROUPS["ohlc_shape_volatility"]
        report = audit_feature_rows(
            [
                {
                    "origin_at": "2025-01-01T01:00:00Z",
                    "features": {name: 1 for name in group_features},
                    "feature_capture_times": {
                        name: "2025-01-01T00:00:00Z"
                        for name in group_features
                    },
                    "feature_vintages": {
                        name: "2025-01-01T00:00:00Z"
                        for name in group_features
                    },
                },
                {"origin_at": "2025-01-01T02:00:00Z", "features": {}},
            ]
        )
        group = report["groups"]["ohlc_shape_volatility"]
        self.assertEqual(group["all_origin_count"], 2)
        self.assertEqual(group["common_available_origin_count"], 1)
        self.assertEqual(group["common_available_origin_indices"], [0])
        self.assertEqual(group["missing_by_feature"]["range_24h_avg_pct"], 1)

    def test_candidate_groups_are_bounded_and_covariates_remain_blocked(self) -> None:
        self.assertEqual(
            set(CANDIDATE_GROUPS),
            {"volume_transform", "ohlc_shape_volatility", "calendar", "external_eth_derivatives"},
        )
        report = audit_feature_rows([])
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["groups"]["external_eth_derivatives"]["status"], "blocked_no_rows")
        self.assertEqual(report["external_data_status"], "blocked_no_point_in_time_external_corpus")
        self.assertEqual(report["covariate_api"]["status"], "blocked_unsupported_unverified")
        self.assertIsNone(report["performance_claim"])
        self.assertEqual(report["execution_parity"]["status"], "not_measured")
        self.assertIn("helper-level fallback", report["execution_parity"]["reason"])

    def test_ohlc_technical_group_contains_registered_market_features(self) -> None:
        expected = {
            "range_24h_avg_pct",
            "volatility_6h_pct",
            "volatility_24h_pct",
            "volatility_7d_pct",
            "rsi_14",
            "momentum_6h_pct",
            "momentum_24h_pct",
            "momentum_7d_pct",
        }
        group = CANDIDATE_GROUPS["ohlc_shape_volatility"]

        self.assertEqual(group, expected)
        self.assertLessEqual(group, set(FEATURE_REGISTRY))
        self.assertLessEqual(group, set(MARKET_FEATURE_NAMES))

    def test_external_group_includes_registered_open_interest_changes_and_common_cohort(
        self,
    ) -> None:
        feature_names = [*MARKET_FEATURE_NAMES, *CANDIDATE_GROUPS["external_eth_derivatives"]]
        row = {
            "origin_at": "2025-01-01T01:00:00Z",
            "features": {name: 1.0 for name in feature_names},
            "feature_capture_times": {name: "2025-01-01T00:00:00Z" for name in feature_names},
            "feature_vintages": {name: "2025-01-01T00:00:00Z" for name in feature_names},
        }
        report = audit_feature_rows([row])
        group = report["groups"]["external_eth_derivatives"]
        self.assertIn("derivatives_oi_change_1h_pct", group["registered_features"])
        self.assertIn("derivatives_oi_change_24h_pct", group["registered_features"])
        self.assertEqual(group["baseline_common_origin_indices"], [0])
        self.assertEqual(group["candidate_common_origin_indices"], [0])
        self.assertEqual(group["candidate_baseline_common_origin_indices"], [0])

    def test_missing_optional_input_returns_exact_baseline_object(self) -> None:
        baseline = {"point": 4, "interval": (1, 7)}
        self.assertIs(baseline_or_candidate(baseline, None), baseline)
        candidate = {"point": 5}
        self.assertIs(baseline_or_candidate(baseline, candidate), candidate)


if __name__ == "__main__":
    unittest.main()
