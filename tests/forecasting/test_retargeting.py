#!/usr/bin/env python3
"""Tests for decision-aware forecast retargeting."""

import unittest
from btc_timesfm.forecasting.retargeting import (
    direction_weighted_skew,
    interval_optimization,
    abstention_aware_retargeting,
)


class RetargetingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.predictions = {"2h": {"price_usd": 100.0, "q10_usd": 90.0, "q90_usd": 110.0}}
        self.data = {}

    def test_direction_weighted_skew(self) -> None:
        retargeted, metadata = direction_weighted_skew(
            self.predictions, self.data, self.data, self.data
        )
        self.assertEqual(metadata["variant"], "direction_weighted_skew")
        # Add actual logic verification once implemented

    def test_interval_optimization(self) -> None:
        retargeted, metadata = interval_optimization(
            self.predictions, self.data, self.data, self.data
        )
        self.assertEqual(metadata["variant"], "interval_optimization")

    def test_abstention_aware(self) -> None:
        retargeted, metadata = abstention_aware_retargeting(
            self.predictions, self.data, self.data, self.data
        )
        self.assertEqual(metadata["variant"], "abstention_aware_retargeting")


if __name__ == "__main__":
    unittest.main()
