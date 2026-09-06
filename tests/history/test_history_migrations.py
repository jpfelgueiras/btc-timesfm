#!/usr/bin/env python3
"""Tests for versioned and recoverable forecast-history schema migrations."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from btc_timesfm.history.history_migrations import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    Migration,
    migrate_database,
    migration_backup_path,
    schema_diagnostics,
    validate_database,
)
from btc_timesfm.history.history_store import ForecastHistoryStore


V1_SCHEMA = """
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
    FOREIGN KEY (origin_at) REFERENCES forecast_origins(origin_at)
        ON DELETE CASCADE
);
CREATE INDEX idx_predictions_target_pending
    ON forecast_predictions(target_at, actual_target_price_usd);
CREATE INDEX idx_predictions_model_horizon
    ON forecast_predictions(model_name, horizon_hours);
"""


def create_v1_fixture(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(V1_SCHEMA)
        connection.execute("INSERT INTO metadata(key, value) VALUES('schema_version', '1')")
        connection.execute("PRAGMA user_version = 1")
        connection.execute(
            """
            INSERT INTO forecast_origins(
                origin_at, generated_at, source_name, pair, source_price_usd,
                regime, market_features_json, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:02:00+00:00",
                "Kraken hourly OHLC",
                "BTC/USD",
                100.0,
                "range",
                "{}",
                "2026-01-01T00:02:00+00:00",
                "2026-01-01T00:02:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO forecast_predictions(
                origin_at, model_name, horizon_hours, target_at,
                predicted_price_usd, predicted_change_pct
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-01-01T00:00:00+00:00",
                "ensemble",
                2,
                "2026-01-01T02:00:00+00:00",
                102.0,
                2.0,
            ),
        )


class HistoryMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "forecast_history.sqlite"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_clean_database_migrates_to_current_schema(self) -> None:
        result = migrate_database(self.db_path)
        self.assertEqual(result["schema_version"], CURRENT_SCHEMA_VERSION)
        self.assertEqual(
            [item["version"] for item in result["applied_migrations"]],
            [1, 2, 3, 4],
        )
        self.assertEqual(validate_database(self.db_path)["integrity"], "ok")

    def test_v1_fixture_upgrades_automatically_and_preserves_history(self) -> None:
        create_v1_fixture(self.db_path)
        original_backup = migration_backup_path(self.db_path, 1)

        store = ForecastHistoryStore(self.db_path)

        self.assertEqual(store.verify()["schema_version"], CURRENT_SCHEMA_VERSION)
        self.assertTrue(original_backup.exists())
        rows = store.export_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["predicted_price_usd"], 102.0)
        diagnostics = schema_diagnostics(self.db_path)
        self.assertEqual(
            [item["version"] for item in diagnostics["applied_migrations"]],
            [1, 2, 3, 4],
        )

    def test_migrations_are_idempotent(self) -> None:
        first = migrate_database(self.db_path)
        first_history = first["applied_migrations"]
        second = migrate_database(self.db_path)
        self.assertEqual(second["schema_version"], CURRENT_SCHEMA_VERSION)
        self.assertEqual(second["applied_migrations"], first_history)

        with sqlite3.connect(self.db_path) as connection:
            count = connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        self.assertEqual(count, CURRENT_SCHEMA_VERSION)

    def test_failed_migration_restores_previous_database_bytes(self) -> None:
        create_v1_fixture(self.db_path)
        before = self.db_path.read_bytes()

        def broken_migration(connection: sqlite3.Connection) -> None:
            connection.execute("CREATE TABLE should_rollback(value TEXT)")
            raise RuntimeError("simulated migration failure")

        migrations = (
            MIGRATIONS[0],
            Migration(2, "broken_migration", broken_migration),
        )
        with self.assertRaisesRegex(RuntimeError, "simulated migration failure"):
            migrate_database(self.db_path, migrations=migrations, target_version=2)

        self.assertEqual(self.db_path.read_bytes(), before)
        backup = migration_backup_path(self.db_path, 1)
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_bytes(), before)
        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            should_exist = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='should_rollback'"
            ).fetchone()
        self.assertIsNone(should_exist)

    def test_newer_schema_is_rejected_without_modification(self) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("CREATE TABLE future_data(value TEXT)")
            connection.execute("PRAGMA user_version = 99")
        before = self.db_path.read_bytes()

        with self.assertRaisesRegex(RuntimeError, "newer than supported"):
            migrate_database(self.db_path)

        self.assertEqual(self.db_path.read_bytes(), before)

    def test_registry_must_be_contiguous(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "contiguous versions"):
            migrate_database(
                self.db_path,
                migrations=(Migration(2, "only_v2", lambda connection: None),),
                target_version=2,
            )


if __name__ == "__main__":
    unittest.main()
