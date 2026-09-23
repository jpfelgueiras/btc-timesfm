from __future__ import annotations

import unittest

from btc_timesfm.research.marginal_interval_ablation import preserve_valid_marginals


class MarginalIntervalAblationTests(unittest.TestCase):
    def test_valid_non_nested_horizons_are_preserved(self) -> None:
        predictions = {
            "2h": {"q10_usd": 90.0, "q50_usd": 100.0, "q90_usd": 110.0},
            "4h": {"q10_usd": 95.0, "q50_usd": 105.0, "q90_usd": 115.0},
            "8h": {"q10_usd": 80.0, "q50_usd": 110.0, "q90_usd": 120.0},
        }
        original = {key: dict(value) for key, value in predictions.items()}
        result = preserve_valid_marginals(predictions)
        self.assertEqual(result, predictions)
        self.assertEqual(predictions, original)

    def test_within_horizon_crossing_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "crossed marginal quantiles"):
            preserve_valid_marginals({"2h": {"q10_usd": 100.0, "q50_usd": 90.0, "q90_usd": 110.0}})

    def test_non_finite_quantiles_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be finite"):
            preserve_valid_marginals(
                {"2h": {"q10_usd": 90.0, "q50_usd": float("nan"), "q90_usd": 110.0}}
            )


if __name__ == "__main__":
    unittest.main()
