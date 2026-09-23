"""Real TimesFM contract tests - these run against the actual pinned checkpoint."""

from __future__ import annotations

import os
import unittest
from typing import Any

import numpy as np

# Skip these tests unless explicitly enabled
SKIP_REAL_TIMESFM = os.getenv("BTC_TIMESFM_RUN_REAL_TESTS", "false").lower() not in (
    "true",
    "1",
    "yes",
)

if not SKIP_REAL_TIMESFM:
    try:
        __import__("timesfm3")
        HAS_TIMESFM = True
    except Exception:
        HAS_TIMESFM = False
else:
    HAS_TIMESFM = False


def make_market(count: int = 513, *, start_price: float = 100.0) -> Any:
    """Create minimal market data for testing."""
    from datetime import datetime, timedelta, timezone

    from btc_timesfm.forecasting.forecast_engine import MarketData

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    closes = np.linspace(start_price, start_price + 5.0, count, dtype=np.float32)
    return MarketData(
        timestamps=[int((base + timedelta(hours=i)).timestamp()) for i in range(count)],
        opens=closes - 0.1,
        highs=closes + 0.5,
        lows=closes - 0.5,
        closes=closes,
        volumes=np.linspace(10.0, 20.0, count, dtype=np.float32),
    )


@unittest.skipIf(SKIP_REAL_TIMESFM or not HAS_TIMESFM, "Real TimesFM tests disabled")
class RealTimesFMContractTests(unittest.TestCase):
    """Contract tests that run against the real pinned TimesFM checkpoint."""

    def setUp(self) -> None:
        """Set up real TimesFM model for testing."""
        from btc_timesfm.forecasting.forecast_engine import MODEL_REVISION, load_timesfm

        self.model = load_timesfm()
        # Verify we got the right revision
        self.assertEqual(self.model.config.revision, MODEL_REVISION)

    def test_model_loads_with_correct_revision(self) -> None:
        """Test that the model loads with the pinned revision."""
        from btc_timesfm.forecasting.forecast_engine import MODEL_REVISION

        self.assertEqual(self.model.config.revision, MODEL_REVISION)

    def test_predict_batch_output_contract(self) -> None:
        """Test that predict_batch follows the expected output contract."""
        data = make_market(513)
        returns = data.returns

        # Test with single context
        outputs = list(
            self.model.predict_batch(
                contexts=[returns[-168:]],  # Use smallest context
                horizon=16,
                return_quantiles=True,
                use_symmetric_averaging=False,
            )
        )

        self.assertEqual(len(outputs), 1)
        result = outputs[0]

        # Check output types and shapes
        self.assertIsInstance(result.forecast, np.ndarray)
        self.assertIsInstance(result.quantiles, np.ndarray)
        self.assertEqual(result.forecast.shape, (16,))  # horizon length
        self.assertEqual(result.quantiles.shape, (16, 9))  # horizon x 9 quantiles

        # Check that all values are finite
        self.assertTrue(np.all(np.isfinite(result.forecast)))
        self.assertTrue(np.all(np.isfinite(result.quantiles)))

        # Check that quantiles are ordered (should be sorted by the model)
        for i in range(len(result.quantiles)):
            self.assertTrue(
                np.all(result.quantiles[i][:-1] <= result.quantiles[i][1:]),
                f"Quantiles not sorted at index {i}",
            )

        # Check that median quantile (index 4) matches point forecast
        np.testing.assert_allclose(
            result.forecast,
            result.quantiles[:, 4],
            rtol=1e-5,
            err_msg="Point forecast != median quantile",
        )

    def test_multi_context_forecast_shapes(self) -> None:
        """Test that multi-context forecasting produces expected shapes."""
        from btc_timesfm.forecasting.forecast_engine import (
            CONTEXT_WINDOWS,
            TARGET_HOURS,
            timesfm_multi_context,
        )

        data = make_market(513)  # Enough for all contexts

        forecasts = timesfm_multi_context(self.model, data)

        # Should have forecasts for each context window
        expected_names = {f"timesfm_{window}h" for window in CONTEXT_WINDOWS}
        self.assertEqual(set(forecasts.keys()), expected_names)

        # Each forecast should have all target hours
        for name in expected_names:
            self.assertEqual(set(forecasts[name].keys()), {f"{hour}h" for hour in TARGET_HOURS})

            # Each hour forecast should have the four price points
            for hour_key in forecasts[name]:
                hour_data = forecasts[name][hour_key]
                self.assertEqual(
                    set(hour_data.keys()), {"price_usd", "q10_usd", "q50_usd", "q90_usd"}
                )
                # All should be floats (or convertible to float)
                for key, value in hour_data.items():
                    self.assertIsInstance(value, (float, int, np.floating))

    def test_signed_return_property(self) -> None:
        """Test that signed returns produce expected directional price movements."""
        data = make_market(513, start_price=100.0)
        returns = data.returns

        # Test with positive returns (should increase price)
        positive_returns = np.full_like(returns[-168:], 0.01)  # 1% per hour
        outputs = list(
            self.model.predict_batch(
                contexts=[positive_returns],
                horizon=2,
                return_quantiles=True,
                use_symmetric_averaging=False,
            )
        )
        result = outputs[0]
        # Positive returns should lead to positive forecast (price increase)
        self.assertTrue(
            np.all(result.forecast > 0), "Positive returns should yield positive log returns"
        )

        # Test with negative returns (should decrease price)
        negative_returns = np.full_like(returns[-168:], -0.01)  # -1% per hour
        outputs = list(
            self.model.predict_batch(
                contexts=[negative_returns],
                horizon=2,
                return_quantiles=True,
                use_symmetric_averaging=False,
            )
        )
        result = outputs[0]
        # Negative returns should lead to negative forecast (price decrease)
        self.assertTrue(
            np.all(result.forecast < 0), "Negative returns should yield negative log returns"
        )

    def test_constant_input_produces_flat_forecast(self) -> None:
        """Test that constant input produces approximately flat forecast."""
        data = make_market(513, start_price=100.0)
        returns = data.returns

        # Constant returns (zeros) should produce approximately zero forecast
        constant_returns = np.zeros_like(returns[-168:])
        outputs = list(
            self.model.predict_batch(
                contexts=[constant_returns],
                horizon=4,
                return_quantiles=True,
                use_symmetric_averaging=False,
            )
        )
        result = outputs[0]
        # Forecast should be close to zero (allowing for small numerical errors)
        self.assertTrue(
            np.all(np.abs(result.forecast) < 0.01), "Constant input should yield near-zero forecast"
        )

        # Quantiles should also be near zero
        self.assertTrue(
            np.all(np.abs(result.quantiles) < 0.01),
            "Quantiles should be near zero for constant input",
        )

    def test_quantile_range_ordering(self) -> None:
        """Test that quantiles follow the expected range: 0.1 < 0.5 < 0.9."""
        data = make_market(513)
        returns = data.returns

        outputs = list(
            self.model.predict_batch(
                contexts=[returns[-168:]],
                horizon=8,
                return_quantiles=True,
                use_symmetric_averaging=False,
            )
        )
        result = outputs[0]

        # Check that Q10 <= Q50 <= Q90 for each horizon point
        for i in range(len(result.forecast)):
            self.assertLessEqual(
                result.quantiles[i, 0],  # Q10
                result.quantiles[i, 4],  # Q50 (median)
                f"Q10 > Q50 at index {i}",
            )
            self.assertLessEqual(
                result.quantiles[i, 4],  # Q50 (median)
                result.quantiles[i, 8],  # Q90
                f"Q50 > Q90 at index {i}",
            )


if __name__ == "__main__":
    unittest.main()
