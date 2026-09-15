#!/usr/bin/env python3
"""Tests for regime- and volatility-conditional interval calibration (#155)."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.support.unit_test_stubs import install_timesfm_stub

install_timesfm_stub()

from btc_timesfm.cli import btc_forecast  # noqa: E402
from btc_timesfm.forecasting.adaptive_weighting import attach_persisted_outcomes  # noqa: E402
from btc_timesfm.forecasting.conditional_calibration import (  # noqa: E402
    assign_bucket,
    bucket_calibration_details,
    build_conditional_calibration_section,
    collect_bucket_samples,
    coverage_within_tolerance,
    empirical_coverage,
    realized_vol_bucket,
)
from btc_timesfm.history.history_store import ForecastHistoryStore  # noqa: E402

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def snapshot(
    origin: datetime,
    *,
    actual: float | None,
    regime: str = "range",
    volatility_6h_pct: float = 0.5,
    point: float = 100.0,
    half_width: float = 10.0,
) -> dict:
    horizons = {}
    outcomes = {}
    for hour in (2, 4, 8, 16):
        horizons[f"{hour}h"] = {
            "price_usd": point,
            "q10_usd": point - half_width,
            "q90_usd": point + half_width,
        }
        if actual is not None:
            outcomes[f"{hour}h"] = {"ensemble": {"actual_target_price_usd": actual}}
    return {
        "latest_close_at": origin.isoformat(),
        "regime": regime,
        "market_features": {"volatility_6h_pct": volatility_6h_pct},
        "predictions": horizons,
        "_outcomes": outcomes,
    }


def mature_samples(
    count: int,
    *,
    regime: str = "range",
    volatility_6h_pct: float = 0.5,
    point: float = 100.0,
    start: datetime = START,
    spread: float = 10.0,
) -> list[dict]:
    return [
        snapshot(
            start + timedelta(hours=i),
            actual=point + (i % count) / max(1, count - 1) * spread,
            regime=regime,
            volatility_6h_pct=volatility_6h_pct,
            point=point,
        )
        for i in range(count)
    ]


class ConditionalCalibrationTests(unittest.TestCase):
    def test_bucket_coverage_within_tolerance_for_mature_buckets(self) -> None:
        history = mature_samples(25, regime="range", volatility_6h_pct=0.5)
        history += mature_samples(25, regime="range", volatility_6h_pct=2.0, point=120.0)
        details = bucket_calibration_details(
            history,
            {},
            2,
            regime="range",
            market_features={"volatility_6h_pct": 0.5},
            min_samples=20,
            tolerance=0.10,
        )
        for name in ("range:low", "range:high"):
            entry = details["buckets"][name]
            self.assertEqual(entry["samples"], 25)
            self.assertEqual(entry["mode"], "conformal")
            self.assertEqual(entry["shrinkage"], 0.0)
            self.assertTrue(entry["verified"])
            self.assertTrue(entry["within_tolerance"])
            assert entry["coverage_after"] is not None
            self.assertAlmostEqual(entry["coverage_after"], 0.80, delta=0.10)
        self.assertEqual(details["coverage_violations"]["bucket_count"], 0)

    def test_reassignment_is_origin_time_only_and_never_reads_outcomes(self) -> None:
        base = snapshot(
            START,
            actual=101.0,
            regime="trending",
            volatility_6h_pct=2.0,
            point=100.0,
        )
        blind = {key: value for key, value in base.items() if key != "_outcomes"}
        self.assertEqual(assign_bucket(base), assign_bucket(blind))

        different_outcome = dict(base)
        different_outcome["_outcomes"] = {
            f"{hour}h": {"ensemble": {"actual_target_price_usd": 400.0}} for hour in (2, 4, 8, 16)
        }
        different_predictions = dict(base)
        different_predictions["predictions"] = {
            f"{hour}h": {"price_usd": 999.0, "q10_usd": 1.0, "q90_usd": 2000.0}
            for hour in (2, 4, 8, 16)
        }
        self.assertEqual(assign_bucket(base), assign_bucket(different_outcome))
        self.assertEqual(assign_bucket(base), assign_bucket(different_predictions))

        other_state = dict(base)
        other_state["volatility_6h_pct"] = 0.5
        other_state["market_features"] = {"volatility_6h_pct": 0.5}
        self.assertEqual(assign_bucket(base), "trending:high")
        self.assertEqual(assign_bucket(other_state), "trending:low")
        self.assertEqual(realized_vol_bucket(None), "unknown")

    def test_bucket_assignment_uses_only_origin_time_snapshot_fields(self) -> None:
        first = snapshot(START, actual=100.0, regime="range", volatility_6h_pct=0.5)
        second = dict(first)
        second["_outcomes"] = {}
        third = dict(first)
        third.pop("_outcomes", None)
        fourth = dict(first)
        fourth["regime"] = "high_volatility"
        labels = {
            assign_bucket(first),
            assign_bucket(second),
            assign_bucket(third),
            assign_bucket(fourth),
        }
        self.assertEqual(labels, {"range:low", "high_volatility:low"})

    def test_sparse_bucket_shrinks_toward_marginal_and_reports_honestly(self) -> None:
        history = mature_samples(30, regime="range", volatility_6h_pct=0.5)
        history += mature_samples(4, regime="trending", volatility_6h_pct=0.5, point=120.0)
        details = bucket_calibration_details(
            history,
            {},
            2,
            regime="trending",
            market_features={"volatility_6h_pct": 0.5},
            min_samples=20,
        )
        sparse = details["buckets"]["trending:low"]
        marginal = float(details["marginal"]["multiplier"])
        self.assertEqual(sparse["samples"], 4)
        self.assertEqual(sparse["mode"], "shrunken")
        self.assertGreater(sparse["shrinkage"], 0.0)
        self.assertLess(sparse["shrinkage"], 1.0)
        self.assertFalse(sparse["verified"])
        raw = float(sparse["bucket_multiplier"])
        adjusted = float(sparse["multiplier"])
        self.assertGreaterEqual(adjusted, min(raw, marginal))
        self.assertLessEqual(adjusted, max(raw, marginal))
        expected = 0.20 * raw + 0.80 * marginal
        self.assertAlmostEqual(adjusted, expected, places=4)
        self.assertIn("samples", sparse)
        self.assertIn("shrinkage", sparse)
        self.assertIn("coverage_after", sparse)

    def test_empty_bucket_falls_back_to_marginal_multiplier(self) -> None:
        history = mature_samples(30, regime="range", volatility_6h_pct=0.5)
        details = bucket_calibration_details(
            history,
            {},
            2,
            regime="trending",
            market_features={"volatility_6h_pct": 2.0},
            min_samples=20,
        )
        entry = details["buckets"]["trending:high"]
        self.assertEqual(entry["samples"], 0)
        self.assertEqual(entry["mode"], "marginal_fallback")
        self.assertEqual(entry["shrinkage"], 1.0)
        self.assertEqual(entry["multiplier"], details["marginal"]["multiplier"])
        self.assertFalse(entry["verified"])
        self.assertIsNone(entry["coverage_before"])
        self.assertIsNone(entry["coverage_after"])

    def test_now_cutoff_excludes_future_origins_from_calibration(self) -> None:
        history = mature_samples(20, regime="range", volatility_6h_pct=0.5)
        leak = snapshot(
            START + timedelta(days=30),
            actual=105.0,
            regime="range",
            volatility_6h_pct=0.5,
            half_width=0.1,
        )
        cutoff = START + timedelta(hours=24)
        without_cutoff = collect_bucket_samples(history + [leak], {}, 2, bucket="range:low")
        with_cutoff = collect_bucket_samples(
            history + [leak], {}, 2, bucket="range:low", now=cutoff
        )
        self.assertEqual(len(with_cutoff), 20)
        self.assertGreater(len(without_cutoff), len(with_cutoff))

        marginal_cutoff = bucket_calibration_details(
            history + [leak], {}, 2, min_samples=20, now=cutoff
        )
        self.assertEqual(marginal_cutoff["marginal"]["samples"], 20)

    def test_unmatured_rows_do_not_participate(self) -> None:
        history = mature_samples(20, regime="range", volatility_6h_pct=0.5)
        history.append(
            snapshot(
                START + timedelta(hours=100),
                actual=None,
                regime="range",
                volatility_6h_pct=0.5,
                half_width=0.1,
            )
        )
        samples = collect_bucket_samples(history, {}, 2, bucket="range:low")
        self.assertEqual(len(samples), 20)
        details = bucket_calibration_details(history, {}, 2, min_samples=20)
        self.assertEqual(details["marginal"]["samples"], 20)
        self.assertEqual(details["buckets"]["range:low"]["samples"], 20)

    def test_empirical_coverage_and_tolerance_helpers(self) -> None:
        samples = [
            {"score": 0.2},
            {"score": 0.8},
            {"score": 1.1},
        ]
        self.assertEqual(empirical_coverage(samples, 1.0), 2 / 3)
        self.assertIsNone(empirical_coverage([], 1.0))
        self.assertTrue(coverage_within_tolerance(0.84, target_coverage=0.80, tolerance=0.10))
        self.assertFalse(coverage_within_tolerance(0.95, target_coverage=0.80, tolerance=0.10))
        self.assertFalse(coverage_within_tolerance(None, target_coverage=0.80, tolerance=0.10))

    def test_section_is_json_serializable_with_export_shape(self) -> None:
        history = mature_samples(25, regime="range", volatility_6h_pct=0.5)
        predictions = {
            f"{hour}h": {
                "price_usd": 100.0,
                "q10_usd": 95.0,
                "q90_usd": 105.0,
                "interval_calibration_multiplier": 1.0,
            }
            for hour in (2, 4, 8, 16)
        }
        section = build_conditional_calibration_section(
            history,
            {},
            regime="range",
            market_features={"volatility_6h_pct": 0.5},
            predictions=predictions,
            min_samples=20,
        )
        self.assertEqual(section["version"], 1)
        self.assertEqual(set(section["horizons"]), {"2h", "4h", "8h", "16h"})
        self.assertEqual(section["overall"]["horizon_count"], 4)
        self.assertEqual(section["overall"]["verified_horizons"], 4)
        self.assertEqual(section["overall"]["prediction_horizons_with_base_interval"], 4)
        for hour in ("2h", "4h", "8h", "16h"):
            entry = section["horizons"][hour]
            self.assertEqual(entry["selected_bucket"], "range:low")
            self.assertTrue(entry["selected"]["verified"])
            interval = entry["recalibrated_interval"]
            self.assertIsNotNone(interval)
            assert interval is not None
            self.assertLess(interval["q10_usd"], interval["q50_usd"])
            self.assertLess(interval["q50_usd"], interval["q90_usd"])
            self.assertEqual(entry["base_interval"]["q10_usd"], 95.0)
        self.assertTrue(section["leakage_guard"]["origin_time_only"])
        self.assertEqual(section["leakage_guard"]["reassignment"], "origin_time_only")
        json.dumps(section)

    def test_sparse_buckets_never_claim_precision_in_section(self) -> None:
        history = mature_samples(2, regime="range", volatility_6h_pct=2.0)
        section = build_conditional_calibration_section(
            history,
            {},
            regime="range",
            market_features={"volatility_6h_pct": 2.0},
            min_samples=20,
        )
        self.assertEqual(section["overall"]["verified_horizons"], 0)
        self.assertEqual(section["overall"]["sparse_horizons"], 4)
        for hour in section["horizons"]:
            entry = section["horizons"][hour]
            self.assertIn(entry["selected"]["mode"], {"shrunken", "marginal_fallback"})
            self.assertFalse(entry["selected"]["verified"])
            self.assertGreaterEqual(entry["selected"]["shrinkage"], 0.0)
            self.assertIn("samples", entry["selected"])
            self.assertIn("shrinkage", entry["selected"])

    def test_forecast_json_embedding_via_cli_helper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "history.sqlite"
            store = ForecastHistoryStore(db)
            actuals: dict[int, float] = {}
            for index in range(25):
                origin = START + timedelta(hours=index)
                source_price = 100.0
                forecast_price = 101.0
                market_features = {
                    "volatility_6h_pct": 0.5 if index % 2 == 0 else 2.0,
                }
                store.ingest_snapshot(
                    {
                        "generated_at": origin.isoformat(),
                        "latest_close_at": origin.isoformat(),
                        "latest_close_usd": source_price,
                        "source": "test",
                        "pair": "BTCUSD",
                        "regime": "range",
                        "market_features": market_features,
                        "predictions": {
                            f"{hour}h": {
                                "price_usd": forecast_price,
                                "q10_usd": forecast_price - 5.0,
                                "q90_usd": forecast_price + 5.0,
                            }
                            for hour in (2, 4, 8, 16)
                        },
                    }
                )
                for hour in (2, 4, 8, 16):
                    target_at = origin + timedelta(hours=hour)
                    actuals[int(target_at.timestamp())] = source_price + (index % 5)
            store.enrich_outcomes(actuals)

            snapshots = store.load_snapshots()
            rows = store.export_rows()
            history = attach_persisted_outcomes(snapshots, rows)
            predictions = {
                f"{hour}h": {
                    "price_usd": 101.0,
                    "q10_usd": 96.0,
                    "q90_usd": 106.0,
                    "interval_calibration_multiplier": 1.0,
                }
                for hour in (2, 4, 8, 16)
            }
            section = btc_forecast.build_conditional_calibration(
                history,
                actuals,
                regime="range",
                market_features={"volatility_6h_pct": 2.0},
                predictions=predictions,
                forecast_origin=START + timedelta(hours=24, minutes=59),
            )
            self.assertEqual(section["version"], 1)
            self.assertEqual(set(section["horizons"]), {"2h", "4h", "8h", "16h"})
            self.assertTrue(section["leakage_guard"]["origin_time_only"])
            self.assertIsNotNone(section["horizons"]["2h"]["recalibrated_interval"])
            json.dumps(section)

    def test_invalid_configuration_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            bucket_calibration_details([], {}, 2, target_coverage=1.0)
        with self.assertRaises(ValueError):
            bucket_calibration_details([], {}, 2, min_samples=0)
        with self.assertRaises(ValueError):
            bucket_calibration_details([], {}, 2, tolerance=0.0)
        with self.assertRaises(ValueError):
            bucket_calibration_details([], {}, 2, cuts=(0.0, 1.0))
        with self.assertRaises(ValueError):
            bucket_calibration_details([], {}, 2, cuts=(1.0, 0.5))
        with self.assertRaises(ValueError):
            build_conditional_calibration_section([], {}, tolerance=0.0)


if __name__ == "__main__":
    unittest.main()
