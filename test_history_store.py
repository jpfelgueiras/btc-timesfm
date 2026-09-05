#!/usr/bin/env python3
"""Unit tests for the durable SQLite forecast history store."""

from __future__ import annotations

import csv
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from history_store import ENSEMBLE_MODEL, ForecastHistoryStore, SCHEMA_VERSION


def make_snapshot(
    origin: datetime,
    *,
    generated_offset_minutes: int = 2,
    source_price: float = 100.0,
    ensemble_2h: float = 102.0,
    regime: str = "range",
) -> dict:
    return {
        "generated_at": (origin + timedelta(minutes=generated_offset_minutes)).isoformat(),
        "latest_close_at": origin.isoformat(),
        "latest_close_usd": source_price,
        "source": "Kraken hourly OHLC",
        "pair": "BTC/USD",
        "regime": regime,
        "market_features": {"rsi_14": 55.0, "volatility_24h_pct": 0.8},
        "model_weights": {
            "2h": {"timesfm_168h": 0.6, "persistence": 0.4},
            "4h": {"timesfm_168h": 0.55, "persistence": 0.45},
        },
        "model_predictions": {
            "timesfm_168h": {
                "2h": {"price_usd": 103.0, "q10_usd": 98.0, "q50_usd": 102.0, "q90_usd": 106.0},
                "4h": {"price_usd": 104.0, "q10_usd": 97.0, "q50_usd": 103.0, "q90_usd": 108.0},
            },
            "persistence": {
                "2h": {"price_usd": source_price},
                "4h": {"price_usd": source_price},
            },
        },
        "predictions": {
            "2h": {
                "price_usd": ensemble_2h,
                "change_pct": (ensemble_2h / source_price - 1.0) * 100.0,
                "q10_usd": 98.0,
                "q50_usd": ensemble_2h,
                "q90_usd": 106.0,
                "model_agreement": 0.75,
            },
            "4h": {
                "price_usd": 103.0,
                "change_pct": 3.0,
                "q10_usd": 97.0,
                "q50_usd": 103.0,
                "q90_usd": 108.0,
                "model_agreement": 0.75,
            },
        },
    }


class HistoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "forecast_history.sqlite"
        self.store = ForecastHistoryStore(self.db_path)
        self.origin = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_schema_is_versioned_and_verifiable(self) -> None:
        verification = self.store.verify()
        self.assertEqual(verification["schema_version"], SCHEMA_VERSION)
        self.assertEqual(verification["integrity"], "ok")
        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_snapshot_creates_ensemble_and_model_rows(self) -> None:
        result = self.store.ingest_snapshot(make_snapshot(self.origin))
        self.assertEqual(result["origins_inserted"], 1)
        # 2 horizons x (ensemble + two underlying models)
        self.assertEqual(result["predictions_inserted"], 6)

        rows = self.store.export_rows()
        self.assertEqual(len(rows), 6)
        self.assertEqual({row["model_name"] for row in rows}, {ENSEMBLE_MODEL, "timesfm_168h", "persistence"})
        timesfm = next(
            row for row in rows if row["model_name"] == "timesfm_168h" and row["horizon_hours"] == 2
        )
        self.assertAlmostEqual(timesfm["ensemble_weight"], 0.6)
        ensemble = next(
            row for row in rows if row["model_name"] == ENSEMBLE_MODEL and row["horizon_hours"] == 2
        )
        self.assertAlmostEqual(ensemble["model_agreement"], 0.75)
        self.assertEqual(json.loads(ensemble["market_features_json"])["rsi_14"], 55.0)

    def test_duplicate_origin_is_idempotent_and_preserves_first_prediction(self) -> None:
        first = make_snapshot(self.origin, ensemble_2h=102.0)
        rerun = make_snapshot(self.origin, generated_offset_minutes=20, ensemble_2h=150.0)
        self.store.ingest_snapshot(first)
        result = self.store.ingest_snapshot(rerun)

        self.assertEqual(result["origins_inserted"], 0)
        self.assertEqual(result["predictions_inserted"], 0)
        stats = self.store.stats()
        self.assertEqual(stats["origins"], 1)
        self.assertEqual(stats["predictions"], 6)

        ensemble = next(
            row
            for row in self.store.export_rows()
            if row["model_name"] == ENSEMBLE_MODEL and row["horizon_hours"] == 2
        )
        self.assertEqual(ensemble["predicted_price_usd"], 102.0)
        self.assertEqual(ensemble["generated_at"], first["generated_at"])

    def test_outcomes_only_mature_on_exact_target_timestamp(self) -> None:
        self.store.ingest_snapshot(make_snapshot(self.origin))
        target_2h = int((self.origin + timedelta(hours=2)).timestamp())
        target_4h = int((self.origin + timedelta(hours=4)).timestamp())

        self.assertEqual(self.store.enrich_outcomes({target_2h - 1: 101.0}), 0)
        self.assertEqual(self.store.stats()["matured_predictions"], 0)

        # The exact +2h candle matures all three +2h logical predictions.
        self.assertEqual(self.store.enrich_outcomes({target_2h: 101.0}), 3)
        self.assertEqual(self.store.stats()["matured_predictions"], 3)

        ensemble = next(
            row
            for row in self.store.export_rows()
            if row["model_name"] == ENSEMBLE_MODEL and row["horizon_hours"] == 2
        )
        self.assertEqual(ensemble["actual_target_price_usd"], 101.0)
        self.assertEqual(ensemble["absolute_error_usd"], 1.0)
        self.assertGreater(ensemble["signed_error_pct"], 0.0)
        self.assertEqual(ensemble["direction_correct"], 1)
        self.assertEqual(ensemble["within_q10_q90"], 1)

        # Outcomes are write-once; a later conflicting value cannot rewrite history.
        self.assertEqual(self.store.enrich_outcomes({target_2h: 999.0}), 0)
        unchanged = next(
            row
            for row in self.store.export_rows()
            if row["model_name"] == ENSEMBLE_MODEL and row["horizon_hours"] == 2
        )
        self.assertEqual(unchanged["actual_target_price_usd"], 101.0)

        self.assertEqual(self.store.enrich_outcomes({target_4h: 104.0}), 3)
        self.assertEqual(self.store.stats()["pending_predictions"], 0)

    def test_rolling_cache_migration_is_idempotent(self) -> None:
        snapshots = [
            make_snapshot(self.origin),
            make_snapshot(self.origin + timedelta(hours=2), source_price=101.0, ensemble_2h=102.5),
        ]
        target = int((self.origin + timedelta(hours=2)).timestamp())
        first = self.store.ingest_snapshots(snapshots, {target: 101.0})
        second = self.store.ingest_snapshots(snapshots, {target: 101.0})
        self.assertEqual(first["origins_inserted"], 2)
        self.assertEqual(first["predictions_inserted"], 12)
        self.assertEqual(second["origins_inserted"], 0)
        self.assertEqual(second["predictions_inserted"], 0)
        self.assertEqual(self.store.stats()["origins"], 2)

    def test_load_snapshots_reconstructs_adaptive_history_shape(self) -> None:
        snapshot = make_snapshot(self.origin)
        self.store.ingest_snapshot(snapshot)
        loaded = self.store.load_snapshots()
        self.assertEqual(len(loaded), 1)
        item = loaded[0]
        self.assertEqual(item["latest_close_at"], self.origin.isoformat())
        self.assertEqual(item["regime"], "range")
        self.assertEqual(item["predictions"]["2h"]["price_usd"], 102.0)
        self.assertEqual(item["model_predictions"]["timesfm_168h"]["4h"]["price_usd"], 104.0)
        self.assertAlmostEqual(item["model_weights"]["2h"]["timesfm_168h"], 0.6)

    def test_performance_summary_uses_all_matured_rows(self) -> None:
        second_origin = self.origin + timedelta(hours=6)
        self.store.ingest_snapshot(make_snapshot(self.origin))
        self.store.ingest_snapshot(make_snapshot(second_origin, source_price=105.0, ensemble_2h=106.0))
        actuals = {
            int((self.origin + timedelta(hours=2)).timestamp()): 101.0,
            int((second_origin + timedelta(hours=2)).timestamp()): 107.0,
        }
        self.store.enrich_outcomes(actuals)
        summary = self.store.performance_summary()
        self.assertEqual(summary["2h"]["samples"], 2)
        self.assertEqual(summary["2h"]["models"]["persistence"]["samples"], 2)
        self.assertGreater(summary["2h"]["mae_pct"], 0.0)

    def test_csv_and_jsonl_exports_are_analysis_ready(self) -> None:
        self.store.ingest_snapshot(make_snapshot(self.origin))
        csv_path = Path(self.tmp.name) / "history.csv"
        jsonl_path = Path(self.tmp.name) / "history.jsonl"
        self.assertEqual(self.store.export(csv_path, "csv"), 6)
        self.assertEqual(self.store.export(jsonl_path, "jsonl"), 6)

        with csv_path.open(encoding="utf-8") as handle:
            csv_rows = list(csv.DictReader(handle))
        self.assertEqual(len(csv_rows), 6)
        self.assertIn("actual_target_price_usd", csv_rows[0])
        self.assertIn("market_features_json", csv_rows[0])

        lines = jsonl_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 6)
        self.assertEqual(json.loads(lines[0])["origin_at"], self.origin.isoformat())


if __name__ == "__main__":
    unittest.main()
