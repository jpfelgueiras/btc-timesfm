#!/usr/bin/env python3
"""Tests for reproducible forecast/backtest manifests."""

from __future__ import annotations

import random
import unittest
from datetime import datetime, timezone

import numpy as np

from tests.support.unit_test_stubs import install_timesfm_stub

install_timesfm_stub()

from btc_timesfm.forecasting.experiment_manifest import (  # noqa: E402
    build_experiment_manifest,
    market_data_identity,
    seed_everything,
)
from btc_timesfm.forecasting.forecast_engine import MarketData  # noqa: E402


def make_data(last_close: float = 103.0) -> MarketData:
    return MarketData(
        timestamps=[1_700_000_000, 1_700_003_600, 1_700_007_200],
        opens=np.asarray([100.0, 101.0, 102.0], dtype=np.float32),
        highs=np.asarray([101.0, 102.0, 104.0], dtype=np.float32),
        lows=np.asarray([99.0, 100.0, 101.0], dtype=np.float32),
        closes=np.asarray([100.5, 101.5, last_close], dtype=np.float32),
        volumes=np.asarray([10.0, 11.0, 12.0], dtype=np.float32),
    )


class ExperimentManifestTests(unittest.TestCase):
    def test_market_data_identity_is_stable_and_sensitive_to_values(self) -> None:
        first = market_data_identity(make_data())
        second = market_data_identity(make_data())
        changed = market_data_identity(make_data(104.0))

        self.assertEqual(first, second)
        self.assertEqual(first["candle_count"], 3)
        self.assertNotEqual(first["ohlcv_sha256"], changed["ohlcv_sha256"])

    def test_identical_configuration_has_same_configuration_and_data_ids(self) -> None:
        common = {
            "run_type": "backtest",
            "data": make_data(),
            "data_source": "Binance BTCUSDT 1h",
            "data_pair": "BTC/USDT",
            "run_parameters": {"days_requested": 90, "samples_requested": 60},
            "model_names": ["persistence", "timesfm_168h"],
            "seed": 7,
            "git_sha": "abc123",
        }
        first = build_experiment_manifest(
            **common,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        second = build_experiment_manifest(
            **common,
            created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        self.assertEqual(first["configuration_id"], second["configuration_id"])
        self.assertEqual(first["data_id"], second["data_id"])
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["code"]["git_sha"], "abc123")

    def test_configuration_change_changes_fingerprint(self) -> None:
        base = build_experiment_manifest(
            run_type="backtest",
            data=make_data(),
            data_source="Binance BTCUSDT 1h",
            data_pair="BTC/USDT",
            run_parameters={"samples_requested": 60},
            git_sha="abc123",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        changed = build_experiment_manifest(
            run_type="backtest",
            data=make_data(),
            data_source="Binance BTCUSDT 1h",
            data_pair="BTC/USDT",
            run_parameters={"samples_requested": 80},
            git_sha="abc123",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self.assertNotEqual(base["configuration_id"], changed["configuration_id"])

    def test_feature_set_version_is_recorded_in_configuration(self) -> None:
        manifest = build_experiment_manifest(
            run_type="research",
            data=make_data(),
            data_source="Binance BTCUSDT 1h",
            data_pair="BTC/USDT",
            feature_set_version="feature-set-1234abcd",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(manifest["configuration"]["feature_set_version"], "feature-set-1234abcd")

    def test_seed_everything_replays_python_and_numpy_randomness(self) -> None:
        seed_everything(42)
        first = (random.random(), float(np.random.random()))
        seed_everything(42)
        second = (random.random(), float(np.random.random()))
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
