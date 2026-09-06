#!/usr/bin/env python3
"""Tests for leakage-safe derivatives feature ablation."""

from __future__ import annotations

import unittest

from tests.support.unit_test_stubs import install_timesfm_stub

install_timesfm_stub()

from btc_timesfm.research.derivatives_ablation import eligible_training_rows, walk_forward_ablation  # noqa: E402


class DerivativesAblationTests(unittest.TestCase):
    def test_training_rows_require_matured_target(self) -> None:
        rows = [
            {"origin_timestamp": 1000},
            {"origin_timestamp": 1000 + 3600},
            {"origin_timestamp": 1000 + 2 * 3600},
        ]
        eligible = eligible_training_rows(rows, 1000 + 4 * 3600, 2)
        self.assertEqual([row["origin_timestamp"] for row in eligible], [1000, 4600, 8200])
        eligible = eligible_training_rows(rows, 1000 + 3 * 3600, 2)
        self.assertEqual([row["origin_timestamp"] for row in eligible], [1000, 4600])

    def test_ablation_report_is_deterministic_and_no_lookahead(self) -> None:
        rows = []
        for index in range(40):
            base = float(index) / 100.0
            rows.append(
                {
                    "origin_at": f"2026-01-{index // 24 + 1:02d}T{index % 24:02d}:00:00+00:00",
                    "origin_timestamp": index * 3600,
                    "current_price": 100.0 + index,
                    "market_features": [base, base * 2, 0.1, 50.0, base, base * 3, base * 4],
                    "derivatives_features": [
                        0.01,
                        1_000_000.0 + index * 1000,
                        base,
                        base * 2,
                        10.0,
                        20.0,
                        30.0,
                        1.0 / 3.0,
                    ],
                    "targets": {
                        "2h": 0.001 + base / 100,
                        "4h": 0.002 + base / 100,
                        "8h": 0.003 + base / 100,
                        "16h": 0.004 + base / 100,
                    },
                }
            )
        first = walk_forward_ablation(rows, min_train=8)
        second = walk_forward_ablation(rows, min_train=8)
        self.assertEqual(first, second)
        self.assertFalse(first["uses_future_information"])
        self.assertEqual(first["method"], "paired_leakage_safe_walk_forward_ridge_ablation")
        for horizon, item in first["by_horizon"].items():
            self.assertGreater(item["market_only"]["samples"], 0, horizon)
            self.assertEqual(
                item["market_only"]["samples"], item["market_plus_derivatives"]["samples"]
            )
            self.assertEqual(len(item["origins"]), item["market_only"]["samples"])


if __name__ == "__main__":
    unittest.main()
