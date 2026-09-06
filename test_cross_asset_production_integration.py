from __future__ import annotations

import unittest
from datetime import datetime, timezone

import numpy as np

import btc_forecast
from forecast_engine import MarketData


class CrossAssetProductionIntegrationTests(unittest.TestCase):
    def test_cross_asset_snapshot_contract_is_production_safe(self) -> None:
        origin = datetime(2026, 9, 6, 12, tzinfo=timezone.utc)
        timestamps = [int(origin.timestamp()) - 3600, int(origin.timestamp())]
        data = MarketData(
            timestamps=timestamps,
            opens=np.asarray([100.0, 101.0], dtype=np.float32),
            highs=np.asarray([101.0, 102.0], dtype=np.float32),
            lows=np.asarray([99.0, 100.0], dtype=np.float32),
            closes=np.asarray([100.0, 101.0], dtype=np.float32),
            volumes=np.asarray([1.0, 1.0], dtype=np.float32),
        )
        self.assertTrue(hasattr(btc_forecast, "fetch_cross_asset_snapshot"))
        self.assertEqual(btc_forecast.CROSS_ASSET_PATH.name, "cross_asset_signal.json")
        self.assertEqual(data.timestamps[-1], int(origin.timestamp()))


if __name__ == "__main__":
    unittest.main()
