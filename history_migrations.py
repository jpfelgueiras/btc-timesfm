#!/usr/bin/env python3
"""Versioned, recoverable SQLite migrations for durable forecast history."""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable


CURRENT_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _migration_1_initial_history_schema(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS forecast_origins (
            origin_at TEXT PRIMARY KEY,
            generated_at TEXT NOT NULL,
            source_name TEXT,
            pair TEXT,
            source_price_usd REAL NOT NULL,
            regime TEXT,
            market_features_json TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS forecast_predictions (
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
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_predictions_target_pending
            ON forecast_predictions(target_at, actual_target_price_usd)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_predictions_model_horizon
            ON forecast_predictions(model_name, horizon_hours)
        """,
    )
    for statement in statements:
        connection.execute(statement)


def _migration_2_add_migration_audit(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY CHECK (version > 0),
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO schema_migrations(version, name, applied_at)
        VALUES(1, 'initial_history_schema', ?)
        """,
        (_utc_now_iso(),),
    )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "initial_history_schema", _migration_1_initial_history_schema),
    Migration(2, "add_migration_audit", _migration_2_add_migration_audit),
)


def migration_backup_path(path: Path | str, source_version: int) -> Path:
    db_path = Path(path)
    return db_path.with_name(f"{db_path.name}.pre-migration-v{source_version}.bak")


def _read_schema_version(path: Path) -> int:
    if not path.exists() or path.stat().st_size == 0:
        return 0
    uri = f"file:{path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _validate_registry(
    migrations: Iterable[Migration], target_version: int
) -> tuple[Migration, ...]:
    ordered = tuple(sorted(migrations, key=lambda migration: migration.version))
    versions = [migration.version for migration in ordered]
    expected = list(range(1, target_version + 1))
    if versions != expected:
        raise RuntimeError(
            f"Migration registry must contain ordered contiguous versions {expected}; got {versions}"
        )
    if len({migration.name for migration in ordered}) != len(ordered):
        raise RuntimeError("Migration names must be unique")
    return ordered


def _set_version(connection: sqlite3.Connection, migration: Migration) -> None:
    if _table_exists(connection, "metadata"):
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
            (str(migration.version),),
        )
    connection.execute(f"PRAGMA user_version = {migration.version}")
    if _table_exists(connection, "schema_migrations"):
        connection.execute(
            """
            INSERT OR IGNORE INTO schema_migrations(version, name, applied_at)
            VALUES(?, ?, ?)
            """,
            (migration.version, migration.name, _utc_now_iso()),
        )


def schema_diagnostics(path: Path | str) -> dict[str, object]:
    db_path = Path(path)
    version = _read_schema_version(db_path)
    migrations: list[dict[str, object]] = []
    if db_path.exists() and db_path.stat().st_size > 0:
        uri = f"file:{db_path.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            if _table_exists(connection, "schema_migrations"):
                rows = connection.execute(
                    "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
                ).fetchall()
                migrations = [
                    {
                        "version": int(row["version"]),
                        "name": str(row["name"]),
                        "applied_at": str(row["applied_at"]),
                    }
                    for row in rows
                ]
    return {
        "schema_version": version,
        "supported_schema_version": CURRENT_SCHEMA_VERSION,
        "applied_migrations": migrations,
    }


def validate_database(
    path: Path | str, expected_version: int = CURRENT_SCHEMA_VERSION
) -> dict[str, object]:
    db_path = Path(path)
    uri = f"file:{db_path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        metadata_version = None
        if "metadata" in tables:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            metadata_version = int(row[0]) if row is not None else None

        migration_versions: list[int] = []
        if "schema_migrations" in tables:
            migration_versions = [
                int(row[0])
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            ]

    if integrity != "ok":
        raise RuntimeError(f"SQLite integrity check failed: {integrity}")
    if foreign_keys:
        raise RuntimeError(f"SQLite foreign-key check failed: {len(foreign_keys)} violation(s)")
    if version != expected_version:
        raise RuntimeError(f"Unexpected schema version {version}; expected {expected_version}")

    required_tables = {"metadata", "forecast_origins", "forecast_predictions"}
    if expected_version >= 2:
        required_tables.add("schema_migrations")
    missing = sorted(required_tables - tables)
    if missing:
        raise RuntimeError(f"History database is missing required tables: {', '.join(missing)}")
    if metadata_version != version:
        raise RuntimeError(
            f"Schema metadata version {metadata_version} does not match PRAGMA user_version {version}"
        )
    if expected_version >= 2:
        expected_migrations = list(range(1, expected_version + 1))
        if migration_versions != expected_migrations:
            raise RuntimeError(
                f"Migration audit is incomplete: expected {expected_migrations}, got {migration_versions}"
            )

    return {
        "integrity": integrity,
        "foreign_key_violations": 0,
        **schema_diagnostics(db_path),
    }


def _remove_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            sidecar.unlink()


def migrate_database(
    path: Path | str,
    *,
    migrations: Iterable[Migration] = MIGRATIONS,
    target_version: int = CURRENT_SCHEMA_VERSION,
) -> dict[str, object]:
    """Upgrade ``path`` atomically and keep a byte-for-byte rollback copy.

    Existing databases are copied before the first schema change. Any migration
    or validation failure restores that copy before the exception is re-raised.
    Clean-database failures remove the partially created database instead.
    """

    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = _validate_registry(migrations, target_version)
    source_version = _read_schema_version(db_path)
    if source_version > target_version:
        raise RuntimeError(
            f"History database schema {source_version} is newer than supported {target_version}"
        )

    existed = db_path.exists() and db_path.stat().st_size > 0
    backup: Path | None = None
    if source_version < target_version and existed:
        backup = migration_backup_path(db_path, source_version)
        shutil.copy2(db_path, backup)

    if source_version == target_version:
        return validate_database(db_path, target_version)

    try:
        connection = sqlite3.connect(db_path)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("BEGIN IMMEDIATE")
            for migration in ordered:
                if migration.version <= source_version:
                    continue
                migration.apply(connection)
                _set_version(connection, migration)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        result = validate_database(db_path, target_version)
        if backup is not None:
            result["migration_backup"] = str(backup)
        return result
    except Exception:
        _remove_sqlite_sidecars(db_path)
        if backup is not None and backup.exists():
            shutil.copy2(backup, db_path)
        elif not existed and db_path.exists():
            db_path.unlink()
        _remove_sqlite_sidecars(db_path)
        raise
