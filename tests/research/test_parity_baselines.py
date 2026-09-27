from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np

from btc_timesfm.forecasting.benchmarks import BENCHMARK_NAMES
from btc_timesfm.forecasting.forecast_engine import MarketData
from btc_timesfm.forecasting.experiment_manifest import market_data_identity
from btc_timesfm.research.parity_baselines import (
    build_baseline_attempts,
    market_window_sha256,
    production_attempts_from_cohort,
    replay_frozen_cohort,
)
from btc_timesfm.research.shadow_deployment import ShadowStore, run_shadow


class ParityBaselineCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.origin = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.market = MarketData(
            timestamps=[
                int((self.origin - timedelta(hours=23 - i)).timestamp()) for i in range(24)
            ],
            opens=np.arange(100, 124, dtype=np.float32),
            highs=np.arange(101, 125, dtype=np.float32),
            lows=np.arange(99, 123, dtype=np.float32),
            closes=np.arange(100, 124, dtype=np.float32),
            volumes=np.ones(24, dtype=np.float32),
        )

    def _build(self, market: MarketData | None = None, capture_id: str = "capture") -> list[dict]:
        return build_baseline_attempts(
            market or self.market,
            origin_at=self.origin.isoformat(),
            configuration_id="champion-config",
            data_lineage_id="data-vintage",
            market_source="exchange",
            market_pair="BTC/USD",
            policy_id="policy-fingerprint",
            production_policy_sha256="d" * 64,
            code_sha="a" * 40,
            model_identity={
                "id": "timesfm",
                "revision": "revision",
                "package": "timesfm",
                "package_version": "1",
            },
            capture_id=capture_id,
        )

    def test_all_registered_baselines_use_origin_limited_market_and_exact_targets(self) -> None:
        rows = self._build()
        self.assertEqual(len(rows), len(BENCHMARK_NAMES) * 4)
        self.assertEqual({row["benchmark_id"] for row in rows}, set(BENCHMARK_NAMES))
        for row in rows:
            self.assertEqual(datetime.fromisoformat(row["origin_at"]), self.origin)
            horizon = int(row["horizon"][:-1])
            self.assertEqual(
                datetime.fromisoformat(row["target_at"]), self.origin + timedelta(hours=horizon)
            )
            self.assertEqual(row["raw_prediction"], row["final_prediction"])
            self.assertEqual(row["source_window_sha256"], market_window_sha256(self.market))
            self.assertEqual(row["status"], "scored")
        self.assertEqual(
            market_window_sha256(self.market), market_data_identity(self.market)["ohlcv_sha256"]
        )

    def test_future_market_data_is_rejected(self) -> None:
        future = MarketData(
            timestamps=[
                *self.market.timestamps,
                int((self.origin + timedelta(hours=1)).timestamp()),
            ],
            opens=np.append(self.market.opens, 124),
            highs=np.append(self.market.highs, 125),
            lows=np.append(self.market.lows, 123),
            closes=np.append(self.market.closes, 124),
            volumes=np.append(self.market.volumes, 1),
        )
        attempts = self._build(future)
        self.assertEqual(len(attempts), len(BENCHMARK_NAMES) * 4)
        self.assertTrue(all(attempt["status"] == "failed" for attempt in attempts))
        self.assertTrue(
            all(attempt["failure_type"] == "PointInTimeValidationError" for attempt in attempts)
        )

    def test_missing_registered_baseline_predictions_are_failure_attempts(self) -> None:
        with patch(
            "btc_timesfm.research.parity_baselines.benchmark_forecasts",
            return_value={"persistence": {"2h": {"price_usd": 123.0}}},
        ):
            rows = self._build()
        self.assertEqual(len(rows), len(BENCHMARK_NAMES) * 4)
        failures = [row for row in rows if row["status"] == "failed"]
        self.assertEqual(len(failures), len(BENCHMARK_NAMES) * 4 - 1)
        self.assertTrue(all(row["final_prediction"] is None for row in failures))

    def test_attempt_persistence_is_immutable_and_retries_are_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ShadowStore(Path(directory) / "shadow.sqlite")
            config, _ = store.register_configuration(
                name="production", parameters={}, role="champion"
            )
            attempts = self._build()
            for row in attempts:
                row["configuration_id"] = config["configuration_id"]
            first = store.record_parity_baseline_attempts(attempts)
            repeated = store.record_parity_baseline_attempts(attempts)
            self.assertEqual(first, repeated)
            self.assertEqual(len(store.load_parity_baseline_attempts()), len(attempts))
            changed = dict(attempts[0])
            changed["final_prediction"] = {"price_usd": 1}
            with self.assertRaisesRegex(ValueError, "different content"):
                store.record_parity_baseline_attempts([changed])
            retried = [dict(row, attempt_id=f"retry:{i}") for i, row in enumerate(attempts)]
            store.record_parity_baseline_attempts(retried)
            self.assertEqual(len(store.load_parity_baseline_attempts()), 2 * len(attempts))

    def test_shadow_capture_uses_the_market_data_supplied_to_public_forecast(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ShadowStore(Path(directory) / "shadow.sqlite")
            origin = self.origin.isoformat()
            production = {
                "latest_close_at": origin,
                "latest_close_usd": float(self.market.closes[-1]),
                "source_pair": "BTC/USD",
                "source": "exchange",
                "pair": "BTC/USD",
                "predictions": {f"{hour}h": {"price_usd": 123.0} for hour in (2, 4, 8, 16)},
                "model_predictions": {},
                "model_weights": {},
                "experiment_manifest": {
                    "run_id": "run-id",
                    "data_id": "data-vintage",
                    "data": {
                        "latest_close_at": origin,
                        "source": "exchange",
                        "pair": "BTC/USD",
                    },
                    "model": {
                        "id": "timesfm",
                        "revision": "revision",
                        "package": "timesfm",
                        "package_version": "1",
                    },
                    "code": {"git_sha": "a" * 40},
                    "configuration": {"policy": {"policy": "production"}},
                },
            }
            report = run_shadow(
                store,
                production,
                {},
                configs=[],
                generated_at=origin,
                market_data=self.market,
            )
            self.assertEqual(
                report["parity_baseline_coverage"], {"attempted": 24, "scored": 24, "failed": 0}
            )
            self.assertEqual(len(store.load_parity_baseline_attempts()), 24)

    def test_replay_keeps_failed_missing_and_unmatched_attempts_and_stays_null(self) -> None:
        pair = {
            "configuration_id": "champion",
            "origin_at": self.origin.isoformat(),
            "target_at": (self.origin + timedelta(hours=2)).isoformat(),
            "horizon": "2h",
            "data_lineage_id": "data-vintage",
            "matured": True,
        }
        production = {
            "configuration_id": "champion",
            "origin_at": pair["origin_at"],
            "target_at": pair["target_at"],
            "horizon": "2h",
            "status": "scored",
            "data_lineage_id": "data-vintage",
            "market_pair": "BTC/USD",
            "market_source": "exchange",
            "source_window_sha256": "b" * 64,
            "policy_id": "policy",
            "code_sha": "c" * 40,
            "model_identity": {"id": "m", "revision": "r", "package": "p", "package_version": "1"},
            "actual_source_identity": "actual-source",
        }
        failed_baseline = {
            **production,
            "benchmark_id": "persistence",
            "status": "failed",
            "failure_message": "deterministic failure",
        }
        unmatched = {**production, "origin_at": (self.origin - timedelta(hours=1)).isoformat()}
        report = replay_frozen_cohort(
            {"pairs": [pair], "cohort_sha256": "cohort-hash"},
            [production],
            [failed_baseline, unmatched],
        )
        self.assertEqual(report["status"], "blocked")
        self.assertIsNone(report["metrics"])
        self.assertEqual(
            report["attempt_rows"][0]["models"]["persistence"]["attempts"][0]["status"], "failed"
        )
        self.assertEqual(len(report["unmatched_attempts"]), 1)
        self.assertIn("unmatched_attempt_rows_present", report["blockers"])

    def test_frozen_production_attempt_uses_exact_cohort_target_and_prediction(self) -> None:
        pair = {
            "configuration_id": "champion",
            "origin_at": self.origin.isoformat(),
            "target_at": (self.origin + timedelta(hours=2)).isoformat(),
            "horizon": "2h",
            "matured": True,
            "production_raw_prediction": {"timesfm": {"price_usd": 124.0}},
            "production_final_prediction": {"price_usd": 125.0},
            "actual_source_identity": "actual-source",
        }
        attempt = production_attempts_from_cohort({"pairs": [pair]})[0]
        self.assertEqual(attempt["origin_at"], pair["origin_at"])
        self.assertEqual(attempt["target_at"], pair["target_at"])
        self.assertEqual(attempt["raw_prediction"], pair["production_raw_prediction"])
        self.assertEqual(attempt["final_prediction"], pair["production_final_prediction"])
        self.assertEqual(attempt["status"], "scored")


if __name__ == "__main__":
    unittest.main()
