#!/usr/bin/env python3
"""Tests for forecast-history integrity audit and repair tooling."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from history_audit import audit_database, load_actuals
from history_migrations import CURRENT_SCHEMA_VERSION


SCHEMA_SQL = """
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE forecast_origins (
    origin_at TEXT PRIMARY KEY,
    generated_at TEXT NOT NULL,
    source_name TEXT,
    pair TEXT,
    source_price_usd REAL NOT NULL,
    regime TEXT,
    market_features_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE forecast_predictions (
    origin_at TEXT NOT NULL,
    model_name TEXT NOT NULL,
    horizon_hours INTEGER NOT NULL CHECK (horizon_hours > 0),
    target_at TEXT NOT NULL,
    predicted_price_usd REAL NOT NULL,
    predicted_change_pct REAL NOT NULL,
    q10_usd REAL,
    q50_usd REAL,
    q90_usd REAL,
    model_agreement REAL,
    ensemble_weight REAL,
    actual_target_price_usd REAL,
    absolute_error_usd REAL,
    absolute_error_pct REAL,
    signed_error_pct REAL,
    actual_change_pct REAL,
    direction_correct INTEGER,
    within_q10_q90 INTEGER,
    matured_at TEXT,
    PRIMARY KEY (origin_at, model_name, horizon_hours),
    FOREIGN KEY (origin_at) REFERENCES forecast_origins(origin_at) ON DELETE CASCADE
);
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""


class HistoryAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "history.sqlite"
        self.origin = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
        self.now = self.origin + timedelta(hours=24)
        self._create_database()
        self._insert_origin(self.origin)
        for model in ("ensemble", "timesfm_168h", "persistence"):
            self._insert_prediction(self.origin, model, 2)
            self._insert_prediction(self.origin, model, 4)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _create_database(self) -> None:
        with sqlite3.connect(self.db) as connection:
            connection.executescript(SCHEMA_SQL)
            connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(CURRENT_SCHEMA_VERSION),),
            )
            connection.executemany(
                """
                INSERT INTO schema_migrations(version, name, applied_at)
                VALUES (?, ?, ?)
                """,
                [
                    (version, f"fixture_v{version}", self.origin.isoformat())
                    for version in range(1, CURRENT_SCHEMA_VERSION + 1)
                ],
            )

    def _insert_origin(self, origin: datetime, source_price: float = 100.0) -> None:
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                """
                INSERT INTO forecast_origins(
                    origin_at, generated_at, source_name, pair, source_price_usd,
                    regime, market_features_json, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    origin.isoformat(),
                    (origin + timedelta(minutes=2)).isoformat(),
                    "Kraken",
                    "BTC/USD",
                    source_price,
                    "range",
                    json.dumps({"rsi_14": 50.0}),
                    origin.isoformat(),
                    origin.isoformat(),
                ),
            )

    def _insert_prediction(self, origin: datetime, model: str, horizon: int) -> None:
        source = 100.0
        predicted = source + horizon
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                """
                INSERT INTO forecast_predictions(
                    origin_at, model_name, horizon_hours, target_at,
                    predicted_price_usd, predicted_change_pct,
                    q10_usd, q50_usd, q90_usd, model_agreement, ensemble_weight
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    origin.isoformat(),
                    model,
                    horizon,
                    (origin + timedelta(hours=horizon)).isoformat(),
                    predicted,
                    (predicted / source - 1.0) * 100.0,
                    95.0 if model == "ensemble" else None,
                    predicted if model == "ensemble" else None,
                    110.0 if model == "ensemble" else None,
                    0.8 if model == "ensemble" else None,
                    None if model == "ensemble" else 0.5,
                ),
            )

    def _actuals(self) -> dict[int, float]:
        return {
            int((self.origin + timedelta(hours=2)).timestamp()): 101.0,
            int((self.origin + timedelta(hours=4)).timestamp()): 104.0,
        }

    def _mature_all(self) -> None:
        result = audit_database(
            self.db,
            repair=True,
            actual_by_timestamp=self._actuals(),
            now=self.now,
        )
        message = json.dumps(result, indent=2, sort_keys=True)
        self.assertEqual(result["summary"]["errors"], 0, message)
        self.assertEqual(result["summary"]["warnings"], 0, message)

    def test_healthy_database_has_no_issues_after_outcomes_mature(self) -> None:
        self._mature_all()
        report = audit_database(self.db, now=self.now)
        self.assertTrue(report["healthy"])
        self.assertEqual(report["summary"]["errors"], 0)
        self.assertEqual(report["summary"]["warnings"], 0)
        self.assertEqual(report["sqlite_integrity"], ["ok"])

    def test_dry_run_reports_missing_outcomes_without_writing(self) -> None:
        report = audit_database(
            self.db,
            now=self.now,
            actual_by_timestamp=self._actuals(),
        )
        issue = next(
            item for item in report["issues"] if item["code"] == "missing_matured_outcomes"
        )
        self.assertEqual(issue["severity"], "warning")
        self.assertTrue(issue["repairable"])
        self.assertEqual(report["summary"]["applied_actions"], 0)
        with sqlite3.connect(self.db) as connection:
            matured = connection.execute(
                """
                SELECT COUNT(*) FROM forecast_predictions
                WHERE actual_target_price_usd IS NOT NULL
                """
            ).fetchone()[0]
        self.assertEqual(matured, 0)

    def test_repair_is_backed_up_and_idempotent(self) -> None:
        first = audit_database(
            self.db,
            repair=True,
            actual_by_timestamp=self._actuals(),
            now=self.now,
        )
        self.assertIsNotNone(first["repairs"]["backup"], json.dumps(first, indent=2))
        self.assertEqual(first["summary"]["applied_actions"], 6)
        self.assertTrue(first["healthy"])

        second = audit_database(
            self.db,
            repair=True,
            actual_by_timestamp=self._actuals(),
            now=self.now,
        )
        self.assertEqual(second["summary"]["applied_actions"], 0)
        self.assertIsNone(second["repairs"]["backup"])

    def test_safe_derived_fields_are_repaired(self) -> None:
        self._mature_all()
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                """
                UPDATE forecast_predictions
                SET target_at = ?,
                    predicted_change_pct = 999.0,
                    absolute_error_pct = NULL,
                    direction_correct = NULL,
                    matured_at = NULL
                WHERE origin_at = ?
                  AND model_name = 'ensemble'
                  AND horizon_hours = 2
                """,
                (
                    (self.origin + timedelta(hours=3)).isoformat(),
                    self.origin.isoformat(),
                ),
            )

        dry_run = audit_database(self.db, now=self.now)
        codes = {item["code"] for item in dry_run["issues"]}
        self.assertIn("target_timestamp_mismatch", codes)
        self.assertIn("predicted_change_mismatch", codes)
        self.assertIn("incomplete_or_inconsistent_outcome_metrics", codes)

        repaired = audit_database(self.db, repair=True, now=self.now)
        self.assertTrue(repaired["healthy"])
        self.assertGreaterEqual(repaired["summary"]["applied_actions"], 3)

    def test_orphan_prediction_blocks_repair_and_is_not_deleted(self) -> None:
        with sqlite3.connect(self.db) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                """
                INSERT INTO forecast_predictions(
                    origin_at, model_name, horizon_hours, target_at,
                    predicted_price_usd, predicted_change_pct
                ) VALUES (?, 'timesfm_orphan', 2, ?, 100.0, 0.0)
                """,
                (
                    "2026-08-01T00:00:00+00:00",
                    "2026-08-01T02:00:00+00:00",
                ),
            )

        report = audit_database(self.db, repair=True, now=self.now)
        codes = {item["code"] for item in report["issues"]}
        self.assertIn("foreign_key_violation", codes)
        self.assertIn("orphan_prediction_rows", codes)
        self.assertIn("blocked_reason", report["repairs"])
        with sqlite3.connect(self.db) as connection:
            count = connection.execute(
                """
                SELECT COUNT(*) FROM forecast_predictions
                WHERE model_name = 'timesfm_orphan'
                """
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_orphan_model_group_is_detected(self) -> None:
        second_origin = self.origin + timedelta(hours=6)
        self._insert_origin(second_origin)
        self._insert_prediction(second_origin, "timesfm_168h", 2)
        report = audit_database(self.db, now=self.now)
        issue = next(item for item in report["issues"] if item["code"] == "orphan_model_groups")
        self.assertEqual(issue["severity"], "error")
        self.assertEqual(issue["count"], 1)

    def test_invalid_required_origin_field_is_detected(self) -> None:
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                """
                UPDATE forecast_origins
                SET market_features_json = 'not-json'
                WHERE origin_at = ?
                """,
                (self.origin.isoformat(),),
            )
        report = audit_database(self.db, now=self.now)
        issue = next(item for item in report["issues"] if item["code"] == "invalid_origin_fields")
        self.assertEqual(issue["severity"], "error")
        self.assertIn("invalid_market_features_json", issue["examples"][0]["problems"])

    def test_actuals_loader_accepts_iso_and_unix_keys(self) -> None:
        path = Path(self.tmp.name) / "actuals.json"
        iso = (self.origin + timedelta(hours=2)).isoformat()
        unix = int((self.origin + timedelta(hours=4)).timestamp())
        path.write_text(json.dumps({iso: 101.0, str(unix): 104.0}), encoding="utf-8")
        loaded = load_actuals(path)
        iso_timestamp = int((self.origin + timedelta(hours=2)).timestamp())
        self.assertEqual(loaded[iso_timestamp], 101.0)
        self.assertEqual(loaded[unix], 104.0)


if __name__ == "__main__":
    unittest.main()
