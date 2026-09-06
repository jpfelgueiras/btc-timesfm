from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import numpy as np

from btc_timesfm.data.cross_asset_signals import (
    CROSS_ASSET_FEATURE_NAMES,
    VIX_SERIES,
    fetch_cross_asset_snapshot,
    parse_fred_csv,
    snapshot_from_inputs,
)
from btc_timesfm.forecasting.forecast_engine import MarketData


def _market(start: int, closes: list[float]) -> MarketData:
    timestamps = [start + (index + 1) * 3600 for index in range(len(closes))]
    values = np.asarray(closes, dtype=np.float32)
    return MarketData(
        timestamps=timestamps,
        opens=values.copy(),
        highs=values * 1.001,
        lows=values * 0.999,
        closes=values,
        volumes=np.ones(len(values), dtype=np.float32) * 100,
    )


def _macro_rows(days: int, *, start_day: int = 1, base: float = 20.0) -> list[dict]:
    return [
        {"date": f"2026-08-{start_day + index:02d}", "value": base + index} for index in range(days)
    ]


class CrossAssetSignalTests(unittest.TestCase):
    def test_fred_parser_ignores_missing_values(self) -> None:
        text = "observation_date,VIXCLS\n2026-08-01,20.5\n2026-08-02,.\n2026-08-03,21.0\n"
        self.assertEqual(
            parse_fred_csv(text, VIX_SERIES),
            [
                {"date": "2026-08-01", "value": 20.5},
                {"date": "2026-08-03", "value": 21.0},
            ],
        )

    def test_same_day_macro_value_is_not_visible(self) -> None:
        start = int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp())
        btc = _market(start, [100 + index for index in range(200)])
        eth = _market(start, [50 + index * 0.7 for index in range(200)])
        origin = datetime.fromtimestamp(btc.timestamps[-1], tz=timezone.utc)
        same_day = origin.date().isoformat()
        previous_day = origin.date().fromordinal(origin.date().toordinal() - 1).isoformat()
        vix = [{"date": previous_day, "value": 20.0}, {"date": same_day, "value": 99.0}]
        yields = [{"date": previous_day, "value": 4.0}, {"date": same_day, "value": 9.0}]
        snapshot = snapshot_from_inputs(origin, btc, eth, vix, yields, max_macro_age_days=10)
        self.assertEqual(snapshot["features"]["macro_vix_level"], 20.0)
        self.assertEqual(snapshot["features"]["macro_us10y_yield_pct"], 4.0)

    def test_future_eth_candles_are_ignored(self) -> None:
        start = int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp())
        btc = _market(start, [100 + index for index in range(200)])
        eth = _market(start, [50 + index for index in range(201)])
        origin = datetime.fromtimestamp(btc.timestamps[-1], tz=timezone.utc)
        vix = _macro_rows(20, base=20.0)
        yields = _macro_rows(20, base=3.0)
        first = snapshot_from_inputs(origin, btc, eth, vix, yields, max_macro_age_days=20)
        eth.closes[-1] = 100000.0
        second = snapshot_from_inputs(origin, btc, eth, vix, yields, max_macro_age_days=20)
        self.assertEqual(first["features"], second["features"])

    def test_full_history_derives_all_features(self) -> None:
        start = int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp())
        btc = _market(start, [100 + index * 0.3 + (index % 5) * 0.2 for index in range(200)])
        eth = _market(start, [50 + index * 0.2 + (index % 7) * 0.15 for index in range(200)])
        origin = datetime.fromtimestamp(btc.timestamps[-1], tz=timezone.utc)
        vix = _macro_rows(20, base=20.0)
        yields = _macro_rows(20, base=3.0)
        snapshot = snapshot_from_inputs(origin, btc, eth, vix, yields, max_macro_age_days=20)
        self.assertEqual(set(snapshot["features"]), set(CROSS_ASSET_FEATURE_NAMES))
        self.assertTrue(snapshot["available"])

    @patch("btc_timesfm.data.cross_asset_signals.fetch_fred_series")
    @patch("btc_timesfm.data.cross_asset_signals.fetch_kraken_pair_hourly")
    def test_provider_failures_degrade_without_breaking(self, kraken: Mock, fred: Mock) -> None:
        import requests

        start = int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp())
        btc = _market(start, [100 + index for index in range(200)])
        origin = datetime.fromtimestamp(btc.timestamps[-1], tz=timezone.utc)
        kraken.side_effect = requests.RequestException("down")
        fred.side_effect = requests.RequestException("down")
        snapshot = fetch_cross_asset_snapshot(origin, btc)
        self.assertEqual(snapshot["status"], "unavailable")
        self.assertFalse(snapshot["available"])
        self.assertEqual(
            set(snapshot["quality"]["provider_errors"]), {"kraken_eth", "fred_vix", "fred_us10y"}
        )


if __name__ == "__main__":
    unittest.main()
