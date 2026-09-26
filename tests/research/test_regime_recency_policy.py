from __future__ import annotations

import unittest

from btc_timesfm.research.regime_recency_policy import (
    POLICY_CATALOG,
    build_report,
    elapsed_decay_pool,
    label_origin_regimes,
)


class RegimeRecencyPolicyTests(unittest.TestCase):
    def test_origin_labels_follow_irregular_snapshots_and_ignore_outcomes(self) -> None:
        origins = [
            {
                "origin_timestamp": 100,
                "features": {"momentum_24h_pct": 0.0},
                "future_outcome": {"regime": "high_volatility"},
            },
            {
                "origin_timestamp": 10_900,
                "features": {"volatility_24h_pct": 4.0, "volatility_7d_pct": 1.0},
            },
            {"origin_timestamp": 100_000, "features": {"momentum_24h_pct": 0.0}},
        ]
        labels = label_origin_regimes(origins)
        self.assertEqual([row["origin_timestamp"] for row in labels], [100, 10_900, 100_000])
        self.assertEqual(labels[0]["regime"], "range")
        self.assertEqual(labels[1]["regime"], "high_volatility")
        self.assertEqual(labels[2]["regime"], "range")

    def test_origin_labels_reject_nonchronological_rows(self) -> None:
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            label_origin_regimes(
                [{"origin_timestamp": 10, "features": {}}, {"origin_timestamp": 10, "features": {}}]
            )

    def test_elapsed_pooling_uses_only_matured_observations_and_falls_back_by_ess(self) -> None:
        observations = [
            {"available_at": 0, "value": 2.0},
            {"available_at": 3600, "value": 4.0},
            {"available_at": 10_000, "value": 1000.0},
        ]
        result = elapsed_decay_pool(
            observations,
            available_at=3600,
            half_life_hours=1.0,
            minimum_ess=1.0,
            fallback_value=9.0,
        )
        self.assertEqual(result["observations"], 2)
        self.assertAlmostEqual(result["raw_estimate"], 10.0 / 3.0)
        self.assertFalse(result["used_fallback"])
        fallback = elapsed_decay_pool(
            observations,
            available_at=3600,
            half_life_hours=1.0,
            minimum_ess=3.0,
            fallback_value=9.0,
        )
        self.assertEqual(fallback["estimate"], 9.0)
        self.assertTrue(fallback["used_fallback"])

    def test_report_is_blocked_without_corpus_and_frozen_base_and_never_promotes(self) -> None:
        report = build_report()
        self.assertEqual(report["decision"], "blocked")
        self.assertFalse(report["promotion_eligible"])
        self.assertIsNone(report["results"])
        self.assertEqual(len(POLICY_CATALOG), 4)
        ready = build_report(
            eligible_corpus=True,
            frozen_base_policy=True,
            matured_paired_outcomes=True,
            nested_walk_forward=True,
        )
        self.assertEqual(ready["decision"], "inconclusive")
        self.assertFalse(ready["promotion_eligible"])


if __name__ == "__main__":
    unittest.main()
