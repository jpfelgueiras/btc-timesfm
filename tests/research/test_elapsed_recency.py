from __future__ import annotations

import unittest

from btc_timesfm.research.elapsed_recency import elapsed_time_weights, effective_sample_size


class ElapsedRecencyTests(unittest.TestCase):
    def test_half_life_is_time_based_for_irregular_origins(self) -> None:
        cutoff = 10 * 3600
        weights = elapsed_time_weights(
            [cutoff - 3600, cutoff - 2 * 3600],
            available_at=cutoff,
            half_life_hours=1.0,
        )
        self.assertAlmostEqual(weights[0], 0.5)
        self.assertAlmostEqual(weights[1], 0.25)

    def test_future_outcomes_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "after available_at"):
            elapsed_time_weights([101], available_at=100, half_life_hours=24)

    def test_effective_sample_size_handles_concentration_and_empty(self) -> None:
        self.assertAlmostEqual(effective_sample_size([1.0, 1.0]), 2.0)
        self.assertAlmostEqual(effective_sample_size([1.0, 0.0]), 1.0)
        self.assertEqual(effective_sample_size([]), 0.0)

    def test_invalid_configuration_fails(self) -> None:
        with self.assertRaises(ValueError):
            elapsed_time_weights([10], available_at=10, half_life_hours=0)
        with self.assertRaises(ValueError):
            effective_sample_size([float("nan")])


if __name__ == "__main__":
    unittest.main()
