"""Tests for the bounded incremental feature evidence gate."""

from __future__ import annotations

import unittest

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
        report = audit_feature_rows(
            [
                {
                    "origin_at": "2025-01-01T01:00:00Z",
                    "features": {
                        "range_24h_avg_pct": 1,
                        "volatility_6h_pct": 2,
                        "volatility_24h_pct": 3,
                        "volatility_7d_pct": 4,
                    },
                    "feature_capture_times": {
                        name: "2025-01-01T00:00:00Z"
                        for name in (
                            "range_24h_avg_pct",
                            "volatility_6h_pct",
                            "volatility_24h_pct",
                            "volatility_7d_pct",
                        )
                    },
                    "feature_vintages": {
                        name: "2025-01-01T00:00:00Z"
                        for name in (
                            "range_24h_avg_pct",
                            "volatility_6h_pct",
                            "volatility_24h_pct",
                            "volatility_7d_pct",
                        )
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

    def test_missing_optional_input_returns_exact_baseline_object(self) -> None:
        baseline = {"point": 4, "interval": (1, 7)}
        self.assertIs(baseline_or_candidate(baseline, None), baseline)
        candidate = {"point": 5}
        self.assertIs(baseline_or_candidate(baseline, candidate), candidate)


if __name__ == "__main__":
    unittest.main()
