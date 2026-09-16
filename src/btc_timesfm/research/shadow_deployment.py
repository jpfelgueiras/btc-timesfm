#!/usr/bin/env python3
"""Run policy-approved research challengers in shadow mode alongside production.

Shadow deployment lets approved research challenger configurations issue live
forecasts next to the production champion without touching the public output.
Each shadow forecast is persisted separately in its own SQLite store keyed by
``(configuration_id, origin_at)`` so manual reruns are idempotent. Outcomes are
matured once the exact target candle exists in the same market data used for
the champion, and matured challenger performance is evaluated against the
champion and persistence on identical origins. Promotion stays impossible until
configured live-observation (sample-count and observation-window) requirements
are met.

The store follows the durable-SQLite conventions of
``btc_timesfm.research.experiment_registry``: ``PRAGMA user_version`` schema
versioning, a ``metadata``/``schema_migrations`` audit trail, byte-for-byte
rollback backups on migration, and bounded append-only tables keyed by
``configuration_id``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, TextIO

import numpy as np

from btc_timesfm.forecasting import adaptive_weighting as aw
from btc_timesfm.forecasting.statistical_significance import (
    DEFAULT_CONFIDENCE,
    paired_bootstrap_comparison,
)
from btc_timesfm.research.champion_challenger import configuration_manifest


CURRENT_SCHEMA_VERSION = 2
SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
SHADOW_REPORT_VERSION = 1
SHADOW_POLICY_VERSION = 1
DEFAULT_DB_PATH = Path(".state/shadow_deployment.sqlite")
DEFAULT_REPORT_PATH = Path("shadow_deployment_report.json")
DEFAULT_SUMMARY_PATH = Path("shadow_deployment_summary.md")
DEFAULT_ACTUALS_PATH = Path(".state/shadow_actuals.json")
HORIZONS = ("2h", "4h", "8h", "16h")
HORIZON_HOURS = (2, 4, 8, 16)
VALID_ROLES = ("champion", "challenger")
VALID_APPROVAL_STATUSES = ("approved", "pending", "rejected")
PRODUCTION_CHAMPION_NAME = "production"

# Candidate parameters that may modify adaptive weighting for a live shadow
# challenger. The keys mirror the optimizer's CandidateConfig so approved
# research challengers behave identically in shadow mode.
_PARAMETER_GLOBALS: dict[str, str] = {
    "min_samples": "ADAPTIVE_MIN_SAMPLES",
    "full_samples": "ADAPTIVE_FULL_SAMPLES",
    "max_blend": "ADAPTIVE_MAX_BLEND",
    "min_weight": "ADAPTIVE_MIN_WEIGHT",
    "max_weight": "ADAPTIVE_MAX_WEIGHT",
    "mae_lambda": "ADAPTIVE_MAE_LAMBDA",
    "direction_reward": "ADAPTIVE_DIRECTION_REWARD",
    "persistence_boost": "PERSISTENCE_FALLBACK_BOOST",
    "target_interval_coverage": "TARGET_INTERVAL_COVERAGE",
    "coverage_penalty": "COVERAGE_PENALTY",
}

_DIRECTION_EPSILON = 1e-9


@dataclass(frozen=True)
class ShadowPolicy:
    """Configurable requirements before a shadow challenger may be promoted."""

    minimum_live_samples: int = 32
    observation_window_days: int = 30
    require_edge_vs_champion: bool = True
    reject_significantly_worse_than_champion: bool = True
    reject_significantly_worse_than_persistence: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.minimum_live_samples, bool) or not isinstance(
            self.minimum_live_samples, int
        ):
            raise ValueError("minimum_live_samples must be a positive integer")
        if self.minimum_live_samples < 1:
            raise ValueError("minimum_live_samples must be a positive integer")
        if isinstance(self.observation_window_days, bool) or not isinstance(
            self.observation_window_days, int
        ):
            raise ValueError("observation_window_days must be a positive integer")
        if self.observation_window_days < 1:
            raise ValueError("observation_window_days must be a positive integer")


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _loads_optional(value: Any) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def _direction(value: float, epsilon: float = _DIRECTION_EPSILON) -> int:
    return 1 if value > epsilon else -1 if value < -epsilon else 0


def shadow_policy_identity(policy: ShadowPolicy) -> str:
    """Stable policy identity; changes when any requirement changes."""
    payload = {"version": SHADOW_POLICY_VERSION, "policy": asdict(policy)}
    return "shadow-policy-" + _sha256_text(_canonical_json(payload))[:16]


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _migration_1_initial_shadow_store(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
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
        CREATE TABLE IF NOT EXISTS configurations (
            configuration_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            parameters TEXT NOT NULL,
            role TEXT NOT NULL
                CHECK (role IN ('champion', 'challenger')),
            approval_status TEXT NOT NULL DEFAULT 'approved'
                CHECK (approval_status IN ('approved', 'pending', 'rejected')),
            approved_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS shadow_forecasts (
            configuration_id TEXT NOT NULL,
            origin_at TEXT NOT NULL,
            latest_close_at TEXT NOT NULL,
            latest_close_usd REAL NOT NULL,
            regime TEXT NOT NULL,
            model_predictions TEXT NOT NULL,
            model_weights TEXT NOT NULL,
            predictions TEXT NOT NULL,
            forecast_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (configuration_id, origin_at),
            FOREIGN KEY (configuration_id)
                REFERENCES configurations(configuration_id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS shadow_outcomes (
            configuration_id TEXT NOT NULL,
            origin_at TEXT NOT NULL,
            horizon TEXT NOT NULL
                CHECK (horizon IN ('2h', '4h', '8h', '16h')),
            actual_price_usd REAL NOT NULL,
            actual_at TEXT NOT NULL,
            absolute_error_pct REAL NOT NULL,
            signed_error_pct REAL NOT NULL,
            direction_correct INTEGER NOT NULL,
            within_q10_q90 INTEGER,
            matured_at TEXT NOT NULL,
            PRIMARY KEY (configuration_id, origin_at, horizon),
            FOREIGN KEY (configuration_id)
                REFERENCES configurations(configuration_id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_shadow_forecasts_origin_at
            ON shadow_forecasts(origin_at)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_shadow_outcomes_matured_at
            ON shadow_outcomes(configuration_id, matured_at)
        """
    )


def _migration_2_monitoring_audit(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(shadow_forecasts)").fetchall()
    }
    if "data_lineage_id" not in columns:
        connection.execute("ALTER TABLE shadow_forecasts ADD COLUMN data_lineage_id TEXT")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS shadow_failures (
            configuration_id TEXT,
            origin_at TEXT,
            stage TEXT NOT NULL,
            error_type TEXT NOT NULL,
            message TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            PRIMARY KEY (configuration_id, origin_at, stage, error_type, message),
            FOREIGN KEY (configuration_id)
                REFERENCES configurations(configuration_id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_shadow_failures_observed_at
            ON shadow_failures(observed_at)
        """
    )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "initial_shadow_store", _migration_1_initial_shadow_store),
    Migration(2, "monitoring_audit", _migration_2_monitoring_audit),
)


def _validate_registry(
    migrations: Iterable[Migration], target_version: int
) -> tuple[Migration, ...]:
    ordered = tuple(sorted(migrations, key=lambda migration: migration.version))
    versions = [migration.version for migration in ordered]
    expected = list(range(1, target_version + 1))
    if versions != expected:
        raise RuntimeError(
            f"Migration registry must contain ordered contiguous versions {expected}; "
            f"got {versions}"
        )
    if len({migration.name for migration in ordered}) != len(ordered):
        raise RuntimeError("Migration names must be unique")
    return ordered


def _read_schema_version(path: Path) -> int:
    if not path.exists() or path.stat().st_size == 0:
        return 0
    uri = f"file:{path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


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


def migration_backup_path(path: Path, source_version: int) -> Path:
    return path.with_name(f"{path.name}.pre-migration-v{source_version}.bak")


def _remove_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            sidecar.unlink()


def schema_diagnostics(path: Path | str) -> dict[str, Any]:
    db_path = Path(path)
    version = _read_schema_version(db_path)
    migrations: list[dict[str, Any]] = []
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
) -> dict[str, Any]:
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

    required_tables = {
        "metadata",
        "schema_migrations",
        "configurations",
        "shadow_forecasts",
        "shadow_outcomes",
        "shadow_failures",
    }
    missing = sorted(required_tables - tables)
    if missing:
        raise RuntimeError(f"Shadow store is missing required tables: {', '.join(missing)}")
    if metadata_version != version:
        raise RuntimeError(
            f"Schema metadata version {metadata_version} does not match "
            f"PRAGMA user_version {version}"
        )
    expected_migrations = list(range(1, expected_version + 1))
    if migration_versions != expected_migrations:
        raise RuntimeError(
            f"Migration audit is incomplete: expected {expected_migrations}, "
            f"got {migration_versions}"
        )
    return {
        "integrity": integrity,
        "foreign_key_violations": 0,
        **schema_diagnostics(db_path),
    }


def migrate_database(
    path: Path | str,
    *,
    migrations: Iterable[Migration] = MIGRATIONS,
    target_version: int = CURRENT_SCHEMA_VERSION,
) -> dict[str, Any]:
    """Create or upgrade the shadow store database with rollback protection."""
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = _validate_registry(migrations, target_version)
    source_version = _read_schema_version(db_path)
    if source_version > target_version:
        raise RuntimeError(
            f"Shadow store schema {source_version} is newer than supported {target_version}"
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


class ShadowStore:
    """SQLite-backed append-safe store for live shadow forecasts."""

    def __init__(self, path: Path | str = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        migrate_database(self.path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = DELETE")
        return connection

    # --- configurations -----------------------------------------------------

    @staticmethod
    def _decode_configuration(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["parameters"] = _loads_optional(record.get("parameters")) or {}
        return record

    def register_configuration(
        self,
        *,
        name: str,
        parameters: Mapping[str, Any] | None = None,
        role: str = "challenger",
        approval_status: str = "approved",
        approved_at: str | None = None,
        created_at: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Register (or reuse) a shadow configuration; first write wins."""
        if not name:
            raise ValueError("name must be a non-empty string")
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role {role!r}; expected one of {VALID_ROLES}")
        if approval_status not in VALID_APPROVAL_STATUSES:
            raise ValueError(
                f"invalid approval_status {approval_status!r}; "
                f"expected one of {VALID_APPROVAL_STATUSES}"
            )
        candidate = {"name": name, "parameters": dict(parameters or {})}
        configuration_id = str(configuration_manifest(candidate, role)["configuration_id"])
        now = _utc_now_iso()
        approved = approved_at or now
        created = created_at or now
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO configurations(
                    configuration_id, name, parameters, role,
                    approval_status, approved_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    configuration_id,
                    name,
                    _canonical_json(dict(parameters or {})),
                    role,
                    approval_status,
                    approved,
                    created,
                    now,
                ),
            )
            created_flag = cursor.rowcount > 0
            row = connection.execute(
                "SELECT * FROM configurations WHERE configuration_id = ?",
                (configuration_id,),
            ).fetchone()
        return self._decode_configuration(row), created_flag

    def set_approval_status(self, configuration_id: str, approval_status: str) -> dict[str, Any]:
        if approval_status not in VALID_APPROVAL_STATUSES:
            raise ValueError(
                f"invalid approval_status {approval_status!r}; "
                f"expected one of {VALID_APPROVAL_STATUSES}"
            )
        now = _utc_now_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE configurations
                SET approval_status = ?, approved_at = ?, updated_at = ?
                WHERE configuration_id = ?
                """,
                (approval_status, now, now, configuration_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"unknown configuration {configuration_id!r}")
            row = connection.execute(
                "SELECT * FROM configurations WHERE configuration_id = ?",
                (configuration_id,),
            ).fetchone()
        return self._decode_configuration(row)

    def get_configuration(self, configuration_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM configurations WHERE configuration_id = ?",
                (configuration_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown configuration {configuration_id!r}")
        return self._decode_configuration(row)

    def list_configurations(
        self,
        *,
        role: str | None = None,
        approval_status: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[str] = []
        if role is not None:
            clauses.append("role = ?")
            params.append(role)
        if approval_status is not None:
            clauses.append("approval_status = ?")
            params.append(approval_status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM configurations{where} ORDER BY created_at, configuration_id",
                tuple(params),
            ).fetchall()
        return [self._decode_configuration(row) for row in rows]

    def champion_configuration(self) -> dict[str, Any]:
        champions = self.list_configurations(role="champion", approval_status="approved")
        if not champions:
            raise KeyError("no approved champion configuration registered")
        return max(champions, key=lambda item: str(item["approved_at"]))

    def approved_challengers(self) -> list[dict[str, Any]]:
        return self.list_configurations(role="challenger", approval_status="approved")

    # --- shadow forecasts ---------------------------------------------------

    @staticmethod
    def _decode_forecast(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["model_predictions"] = _loads_optional(record.get("model_predictions")) or {}
        record["model_weights"] = _loads_optional(record.get("model_weights")) or {}
        record["predictions"] = _loads_optional(record.get("predictions")) or {}
        return record

    def record_forecast(
        self,
        *,
        configuration_id: str,
        origin_at: str,
        latest_close_at: str,
        latest_close_usd: float,
        regime: str,
        model_predictions: Mapping[str, Any],
        model_weights: Mapping[str, Any],
        predictions: Mapping[str, Any],
        forecast_sha256: str,
        data_lineage_id: str | None = None,
        created_at: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Persist one shadow forecast idempotently keyed by (config, origin)."""
        self.get_configuration(configuration_id)
        if not origin_at:
            raise ValueError("origin_at must be a non-empty string")
        now = _utc_now_iso()
        created = created_at or now
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO shadow_forecasts(
                    configuration_id, origin_at, latest_close_at, latest_close_usd,
                    regime, model_predictions, model_weights, predictions,
                    forecast_sha256, data_lineage_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    configuration_id,
                    origin_at,
                    latest_close_at,
                    float(latest_close_usd),
                    regime,
                    _canonical_json(dict(model_predictions)),
                    _canonical_json(dict(model_weights)),
                    _canonical_json(dict(predictions)),
                    forecast_sha256,
                    data_lineage_id,
                    created,
                ),
            )
            created_flag = cursor.rowcount > 0
            row = connection.execute(
                "SELECT * FROM shadow_forecasts WHERE configuration_id = ? AND origin_at = ?",
                (configuration_id, origin_at),
            ).fetchone()
        return self._decode_forecast(row), created_flag

    def load_forecasts(self, configuration_id: str | None = None) -> list[dict[str, Any]]:
        if configuration_id is None:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM shadow_forecasts ORDER BY origin_at, configuration_id"
                ).fetchall()
            return [self._decode_forecast(row) for row in rows]
        self.get_configuration(configuration_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM shadow_forecasts WHERE configuration_id = ? ORDER BY origin_at",
                (configuration_id,),
            ).fetchall()
        return [self._decode_forecast(row) for row in rows]

    def count_forecasts(self, configuration_id: str | None = None) -> int:
        if configuration_id is None:
            with self._connect() as connection:
                return int(
                    connection.execute("SELECT COUNT(*) FROM shadow_forecasts").fetchone()[0]
                )
        self.get_configuration(configuration_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM shadow_forecasts WHERE configuration_id = ?",
                (configuration_id,),
            ).fetchone()
        return int(row[0])

    # --- matured outcomes ---------------------------------------------------

    @staticmethod
    def _decode_outcome(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["direction_correct"] = bool(record["direction_correct"])
        within = record.get("within_q10_q90")
        record["within_q10_q90"] = within if within is None else bool(within)
        return record

    def _load_outcome_rows(
        self, *, configuration_id: str | None = None
    ) -> dict[str, dict[str, dict[str, dict[str, Any]]]]:
        """Group outcomes as {configuration_id: {origin_at: {horizon: row}}}."""
        if configuration_id is not None:
            self.get_configuration(configuration_id)
        clauses = " WHERE configuration_id = ?" if configuration_id is not None else ""
        params: tuple[str, ...] = (configuration_id,) if configuration_id is not None else ()
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM shadow_outcomes{clauses} ORDER BY origin_at, horizon",
                params,
            ).fetchall()
        grouped: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
        for row in rows:
            outcome = self._decode_outcome(row)
            config_id = str(outcome["configuration_id"])
            origin_at = str(outcome["origin_at"])
            horizon = str(outcome["horizon"])
            grouped.setdefault(config_id, {}).setdefault(origin_at, {})[horizon] = outcome
        return grouped

    def load_outcomes(self, *, configuration_id: str | None = None) -> list[dict[str, Any]]:
        grouped = self._load_outcome_rows(configuration_id=configuration_id)
        return [
            outcome
            for by_origin in grouped.values()
            for by_horizon in by_origin.values()
            for outcome in by_horizon.values()
        ]

    def count_outcomes(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM shadow_outcomes").fetchone()[0])

    def record_failure(
        self,
        *,
        stage: str,
        error: Exception,
        configuration_id: str | None = None,
        origin_at: str | None = None,
        observed_at: str | None = None,
    ) -> bool:
        if not stage:
            raise ValueError("stage must be a non-empty string")
        if configuration_id is not None:
            self.get_configuration(configuration_id)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO shadow_failures(
                    configuration_id, origin_at, stage, error_type, message, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    configuration_id,
                    origin_at,
                    stage,
                    type(error).__name__,
                    str(error),
                    observed_at or _utc_now_iso(),
                ),
            )
        return cursor.rowcount > 0

    def load_failures(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM shadow_failures ORDER BY observed_at, stage"
            ).fetchall()
        return [dict(row) for row in rows]

    def mature_outcomes(
        self,
        actual_by_timestamp: Mapping[int, float],
        *,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Fill matured outcomes for any shadow forecast whose target exists."""
        matured_at = now or _utc_now_iso()
        forecasts = self.load_forecasts()
        inserted = 0
        already_matured = 0
        not_available = 0
        by_configuration: dict[str, int] = {}
        horizons_matured: dict[str, int] = {}
        for forecast in forecasts:
            origin = _parse_utc(str(forecast["origin_at"]))
            origin_timestamp = int(origin.timestamp())
            previous_close = float(forecast["latest_close_usd"])
            configuration_id = str(forecast["configuration_id"])
            predictions = forecast["predictions"]
            if not isinstance(predictions, dict):
                continue
            for horizon in HORIZONS:
                item = predictions.get(horizon)
                if not isinstance(item, dict) or "price_usd" not in item:
                    continue
                hour = int(horizon[:-1])
                target_timestamp = origin_timestamp + hour * 3600
                actual = actual_by_timestamp.get(target_timestamp)
                if actual is None or float(actual) <= 0:
                    not_available += 1
                    continue
                actual = float(actual)
                predicted = float(item["price_usd"])
                absolute_error_pct = abs(predicted - actual) / actual * 100.0
                signed_error_pct = (predicted - actual) / actual * 100.0
                direction_correct = _direction(predicted - previous_close) == _direction(
                    actual - previous_close
                )
                within: bool | None = None
                q10 = item.get("q10_usd")
                q90 = item.get("q90_usd")
                if q10 is not None and q90 is not None:
                    try:
                        within = float(q10) <= actual <= float(q90)
                    except (TypeError, ValueError):
                        within = None
                target_at = datetime.fromtimestamp(target_timestamp, tz=timezone.utc).isoformat()
                with self._connect() as connection:
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO shadow_outcomes(
                            configuration_id, origin_at, horizon, actual_price_usd,
                            actual_at, absolute_error_pct, signed_error_pct,
                            direction_correct, within_q10_q90, matured_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            configuration_id,
                            str(forecast["origin_at"]),
                            horizon,
                            actual,
                            target_at,
                            round(absolute_error_pct, 8),
                            round(signed_error_pct, 8),
                            int(direction_correct),
                            None if within is None else int(within),
                            matured_at,
                        ),
                    )
                if cursor.rowcount > 0:
                    inserted += 1
                else:
                    already_matured += 1
                by_configuration[configuration_id] = by_configuration.get(configuration_id, 0) + 1
                horizons_matured[horizon] = horizons_matured.get(horizon, 0) + 1
        return {
            "inserted_outcomes": inserted,
            "already_matured": already_matured,
            "not_yet_available": not_available,
            "examined_forecasts": len(forecasts),
            "by_configuration": by_configuration,
            "by_horizon": horizons_matured,
            "matured_at": matured_at,
        }

    # --- evaluation ---------------------------------------------------------

    def evaluate(
        self,
        configuration_id: str,
        *,
        policy: ShadowPolicy | None = None,
        generated_at: str | None = None,
    ) -> dict[str, Any]:
        active = policy or ShadowPolicy()
        challenger = self.get_configuration(configuration_id)
        if challenger["role"] != "challenger":
            raise ValueError(f"configuration {configuration_id!r} is not a challenger")
        champion = self.champion_configuration()

        challenger_forecasts = {
            str(row["origin_at"]): row for row in self.load_forecasts(configuration_id)
        }
        champion_forecasts = {
            str(row["origin_at"]): row for row in self.load_forecasts(champion["configuration_id"])
        }
        shared_origins = sorted(set(challenger_forecasts) & set(champion_forecasts))
        common_origins = [
            origin_at
            for origin_at in shared_origins
            if challenger_forecasts[origin_at].get("data_lineage_id")
            and challenger_forecasts[origin_at].get("data_lineage_id")
            == champion_forecasts[origin_at].get("data_lineage_id")
        ]

        outcomes = self._load_outcome_rows()
        challenger_outcomes = outcomes.get(configuration_id, {})
        champion_outcomes = outcomes.get(champion["configuration_id"], {})

        metrics, significance, live_samples, fully_matured = _evaluate_series(
            common_origins=common_origins,
            challenger_forecasts=challenger_forecasts,
            champion_forecasts=champion_forecasts,
            challenger_outcomes=challenger_outcomes,
            champion_outcomes=champion_outcomes,
            policy=active,
        )

        matured_by_horizon: dict[str, int] = {}
        for origin_outcomes in challenger_outcomes.values():
            for horizon in origin_outcomes:
                matured_by_horizon[horizon] = matured_by_horizon.get(horizon, 0) + 1
        first_origin = common_origins[0] if common_origins else None
        last_origin = common_origins[-1] if common_origins else None
        observation_window_days: float | None = None
        if first_origin is not None and last_origin is not None:
            observation_window_days = round(
                (_parse_utc(last_origin) - _parse_utc(first_origin)).total_seconds() / 86400.0,
                2,
            )

        maturity = {
            "live_samples": live_samples,
            "fully_matured_samples": fully_matured,
            "matured_horizons": matured_by_horizon,
            "common_origin_count": len(common_origins),
            "identical_data_lineage": len(common_origins) == len(shared_origins),
            "lineage_mismatch_count": len(shared_origins) - len(common_origins),
            "first_origin_at": first_origin,
            "last_origin_at": last_origin,
            "observation_window_days": observation_window_days,
            "checks": {
                "enough_live_samples": live_samples >= active.minimum_live_samples,
                "observation_window_met": (
                    observation_window_days is not None
                    and observation_window_days >= active.observation_window_days
                ),
            },
        }
        promotion = _promotion_gate(maturity, significance, active)
        return {
            "schema_version": SHADOW_REPORT_VERSION,
            "generated_at": generated_at or _utc_now_iso(),
            "challenger": {
                "configuration_id": challenger["configuration_id"],
                "name": challenger["name"],
                "parameters": challenger["parameters"],
                "approval_status": challenger["approval_status"],
                "approved_at": challenger["approved_at"],
            },
            "champion": {
                "configuration_id": champion["configuration_id"],
                "name": champion["name"],
                "parameters": champion["parameters"],
            },
            "policy_id": shadow_policy_identity(active),
            "policy": asdict(active),
            "maturity": maturity,
            "metrics": metrics,
            "significance": significance,
            "promotion": promotion,
        }

    def stats(self) -> dict[str, Any]:
        with self._connect() as connection:
            configurations = int(
                connection.execute("SELECT COUNT(*) FROM configurations").fetchone()[0]
            )
            forecasts = int(
                connection.execute("SELECT COUNT(*) FROM shadow_forecasts").fetchone()[0]
            )
            outcomes = int(connection.execute("SELECT COUNT(*) FROM shadow_outcomes").fetchone()[0])
            failures = int(connection.execute("SELECT COUNT(*) FROM shadow_failures").fetchone()[0])
            first_last = connection.execute(
                "SELECT MIN(origin_at), MAX(origin_at) FROM shadow_forecasts"
            ).fetchone()
            by_role: dict[str, int] = {}
            for row in connection.execute(
                "SELECT role, COUNT(*) AS n FROM configurations GROUP BY role"
            ).fetchall():
                by_role[str(row["role"])] = int(row["n"])
        diagnostics = schema_diagnostics(self.path)
        return {
            "schema_version": diagnostics["schema_version"],
            "supported_schema_version": diagnostics["supported_schema_version"],
            "applied_migrations": diagnostics["applied_migrations"],
            "configurations": configurations,
            "by_role": by_role,
            "shadow_forecasts": forecasts,
            "matured_outcomes": outcomes,
            "observable_failures": failures,
            "first_shadow_origin_at": first_last[0],
            "latest_shadow_origin_at": first_last[1],
            "database_bytes": self.path.stat().st_size if self.path.exists() else 0,
        }

    def verify(self) -> dict[str, Any]:
        return validate_database(self.path)


def _predicted_prices(forecast: Mapping[str, Any]) -> dict[str, float]:
    predictions = forecast.get("predictions")
    if not isinstance(predictions, dict):
        return {}
    result: dict[str, float] = {}
    for horizon in HORIZONS:
        item = predictions.get(horizon)
        if isinstance(item, dict) and isinstance(item.get("price_usd"), (int, float)):
            result[horizon] = float(item["price_usd"])
    return result


def _evaluate_series(
    *,
    common_origins: list[str],
    challenger_forecasts: Mapping[str, Mapping[str, Any]],
    champion_forecasts: Mapping[str, Mapping[str, Any]],
    challenger_outcomes: Mapping[str, Mapping[str, Any]],
    champion_outcomes: Mapping[str, Mapping[str, Any]],
    policy: ShadowPolicy,
) -> tuple[dict[str, Any], dict[str, Any], int, int]:
    def mae_series(
        outcomes: Mapping[str, Mapping[str, Any]],
        predicted_at: Callable[[str], dict[str, float]],
    ) -> dict[str, list[float]]:
        per_horizon: dict[str, list[float]] = {horizon: [] for horizon in HORIZONS}
        for origin_at in common_origins:
            by_horizon = outcomes.get(origin_at, {})
            predicted = predicted_at(origin_at)
            for horizon in HORIZONS:
                outcome = by_horizon.get(horizon)
                price = predicted.get(horizon)
                actual = outcome.get("actual_price_usd") if outcome is not None else None
                if price is None or actual is None:
                    continue
                per_horizon[horizon].append(abs(price - float(actual)) / float(actual) * 100.0)
        return per_horizon

    def mae_objective(
        outcomes: Mapping[str, Mapping[str, Any]],
        predicted_at: Callable[[str], dict[str, float]],
    ) -> list[float]:
        series: list[float] = []
        for origin_at in common_origins:
            by_horizon = outcomes.get(origin_at, {})
            if not all(horizon in by_horizon for horizon in HORIZONS):
                continue
            predicted = predicted_at(origin_at)
            values: list[float] = []
            for horizon in HORIZONS:
                price = predicted.get(horizon)
                actual = by_horizon[horizon].get("actual_price_usd")
                if price is None or actual is None:
                    break
                values.append(abs(price - float(actual)) / float(actual) * 100.0)
            if len(values) == len(HORIZONS):
                series.append(float(np.mean(values)))
        return series

    def champions_predicted_at(origin_at: str) -> dict[str, float]:
        return _predicted_prices(champion_forecasts[origin_at])

    def challengers_predicted_at(origin_at: str) -> dict[str, float]:
        return _predicted_prices(challenger_forecasts[origin_at])

    def persistence_predicted_at(origin_at: str) -> dict[str, float]:
        close = float(champion_forecasts[origin_at]["latest_close_usd"])
        return {horizon: close for horizon in HORIZONS}

    champion_horizon = mae_series(champion_outcomes, champions_predicted_at)
    challenger_horizon = mae_series(challenger_outcomes, challengers_predicted_at)
    persistence_horizon = mae_series(champion_outcomes, persistence_predicted_at)
    champion_objective = mae_objective(champion_outcomes, champions_predicted_at)
    challenger_objective = mae_objective(challenger_outcomes, challengers_predicted_at)
    persistence_objective = mae_objective(champion_outcomes, persistence_predicted_at)

    def summarize(per_horizon: dict[str, list[float]], objective: list[float]) -> dict[str, Any]:
        by_horizon: dict[str, Any] = {}
        for horizon in HORIZONS:
            values = per_horizon.get(horizon, [])
            by_horizon[horizon] = {
                "samples": len(values),
                "mae_pct": float(np.mean(values)) if values else None,
            }
        return {
            "samples": len(objective),
            "mae_pct": float(np.mean(objective)) if objective else None,
            "by_horizon": by_horizon,
        }

    metrics = {
        "champion": summarize(champion_horizon, champion_objective),
        "challenger": summarize(challenger_horizon, challenger_objective),
        "persistence": summarize(persistence_horizon, persistence_objective),
    }

    def compare(candidate_series: list[float], baseline_series: list[float], metric: str) -> Any:
        return paired_bootstrap_comparison(
            candidate_series,
            baseline_series,
            metric=metric,
            lower_is_better=True,
            confidence=DEFAULT_CONFIDENCE,
            min_samples=max(1, policy.minimum_live_samples),
        )

    vs_champion: dict[str, Any] = {"objective": None, "by_horizon": {}}
    if challenger_objective and champion_objective:
        vs_champion["objective"] = compare(challenger_objective, champion_objective, "mae_pct")
    for horizon in HORIZONS:
        candidate_values = challenger_horizon.get(horizon, [])
        baseline_values = champion_horizon.get(horizon, [])
        if candidate_values and baseline_values:
            vs_champion["by_horizon"][horizon] = compare(
                candidate_values, baseline_values, f"{horizon}_mae_pct"
            )

    vs_persistence: dict[str, Any] = {"objective": None, "by_horizon": {}}
    if challenger_objective and persistence_objective:
        vs_persistence["objective"] = compare(
            challenger_objective, persistence_objective, "mae_pct"
        )
    for horizon in HORIZONS:
        candidate_values = challenger_horizon.get(horizon, [])
        baseline_values = persistence_horizon.get(horizon, [])
        if candidate_values and baseline_values:
            vs_persistence["by_horizon"][horizon] = compare(
                candidate_values, baseline_values, f"{horizon}_mae_pct"
            )

    significance = {
        "method": "paired_bootstrap",
        "confidence": DEFAULT_CONFIDENCE,
        "pairing_key": "forecast_origin",
        "identical_origins": len(common_origins),
        "vs_champion": vs_champion,
        "vs_persistence": vs_persistence,
    }

    live_samples = sum(1 for by_horizon in challenger_outcomes.values() if by_horizon)
    fully_matured = sum(
        1 for by_horizon in challenger_outcomes.values() if len(by_horizon) == len(HORIZONS)
    )
    return metrics, significance, live_samples, fully_matured


def _promotion_gate(
    maturity: Mapping[str, Any],
    significance: Mapping[str, Any],
    policy: ShadowPolicy,
) -> dict[str, Any]:
    def conclusion(key: str) -> str | None:
        block = significance.get(key)
        if not isinstance(block, dict):
            return None
        objective = block.get("objective")
        if not isinstance(objective, dict):
            return None
        value = objective.get("conclusion")
        return str(value) if value else None

    vs_champion = conclusion("vs_champion")
    vs_persistence = conclusion("vs_persistence")

    checks = {
        "enough_live_samples": bool(maturity.get("checks", {}).get("enough_live_samples", False)),
        "observation_window_met": bool(
            maturity.get("checks", {}).get("observation_window_met", False)
        ),
        "not_significantly_worse_than_champion": (
            not policy.reject_significantly_worse_than_champion or vs_champion != "baseline_better"
        ),
        "statistical_edge_vs_champion": (
            not policy.require_edge_vs_champion or vs_champion == "candidate_better"
        ),
        "not_significantly_worse_than_persistence": (
            not policy.reject_significantly_worse_than_persistence
            or vs_persistence != "baseline_better"
        ),
    }
    unmet = [name for name, passed in checks.items() if not passed]
    eligible = not unmet
    return {
        "available": vs_champion is not None and vs_persistence is not None,
        "eligible": eligible,
        "decision": "eligible" if eligible else "blocked",
        "reasons": (
            [f"requirement_not_met:{name}" for name in unmet]
            if unmet
            else ["all_shadow_requirements_met"]
        ),
        "checks": checks,
    }


@contextmanager
def _apply_candidate_parameters(parameters: Mapping[str, Any]) -> Iterator[None]:
    """Temporarily install validated challenger parameters in adaptive weighting."""
    names: dict[str, str] = {}
    for key, attr in _PARAMETER_GLOBALS.items():
        if key not in parameters:
            continue
        raw = parameters[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise ValueError(f"invalid numeric value for candidate parameter {key!r}")
        try:
            float(raw)
        except ValueError as exc:
            raise ValueError(f"invalid numeric value for candidate parameter {key!r}") from exc
        names[attr] = str(raw)
    original = {name: getattr(aw, name) for name in names}
    try:
        for attr, value in names.items():
            setattr(aw, attr, float(value))
        yield
    finally:
        for attr, value in original.items():
            setattr(aw, attr, value)


def _ensemble_price(
    current_price: float,
    model_predictions: Mapping[str, Any],
    horizon: str,
    weights: Mapping[str, float],
) -> float:
    active = [
        (name, weight)
        for name, weight in weights.items()
        if weight > 0 and name in model_predictions
    ]
    if not active:
        return current_price
    total = sum(weight for _, weight in active)
    log_change = 0.0
    for name, weight in active:
        price = float(model_predictions[name][horizon]["price_usd"])
        log_change += weight / total * math.log(price / current_price)
    return current_price * math.exp(log_change)


def _history_snapshots(
    store: ShadowStore, configuration_id: str, *, exclude_origin_at: str | None = None
) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for forecast in store.load_forecasts(configuration_id):
        origin_at = str(forecast["origin_at"])
        if exclude_origin_at is not None and origin_at == exclude_origin_at:
            continue
        model_predictions = forecast.get("model_predictions")
        if not isinstance(model_predictions, dict):
            continue
        snapshots.append(
            {
                "latest_close_at": str(forecast["latest_close_at"]),
                "latest_close_usd": float(forecast["latest_close_usd"]),
                "regime": str(forecast["regime"]),
                "model_predictions": model_predictions,
            }
        )
    snapshots.sort(key=lambda item: str(item["latest_close_at"]))
    grouped = store._load_outcome_rows(configuration_id=configuration_id)
    by_origin = grouped.get(configuration_id, {})
    for snapshot in snapshots:
        origin_outcomes = by_origin.get(str(snapshot["latest_close_at"]), {})
        if not origin_outcomes:
            continue
        snapshot["_outcomes"] = {}
        for horizon, outcome in origin_outcomes.items():
            actual = outcome.get("actual_price_usd")
            if actual is None:
                continue
            for model_name in snapshot["model_predictions"]:
                snapshot["_outcomes"].setdefault(horizon, {})[model_name] = {
                    "actual_target_price_usd": float(actual),
                    "matured_at": outcome.get("matured_at"),
                }
    return snapshots


def shadow_predictions(
    parameters: Mapping[str, Any],
    production: Mapping[str, Any],
    actual_by_timestamp: Mapping[int, float],
    history: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Recompute ensemble predictions for one challenger at the current origin."""
    current_price = float(production["latest_close_usd"])
    regime = str(production.get("regime") or "range")
    model_predictions = production.get("model_predictions")
    if not isinstance(model_predictions, dict) or not model_predictions:
        raise ValueError("production snapshot is missing model_predictions")
    production_predictions = production.get("predictions")
    if not isinstance(production_predictions, dict):
        raise ValueError("production snapshot is missing predictions")

    names = list(model_predictions)
    enabled = parameters.get("enabled_models")
    if isinstance(enabled, list) and enabled:
        names = [name for name in names if name in enabled]
    if not names:
        raise ValueError("challenger enabled_models enable no usable models")
    if "persistence" not in names:
        raise ValueError("challenger enabled_models must include persistence")

    raw_history_limit = parameters.get("history_limit")
    history_limit = (
        int(raw_history_limit)
        if isinstance(raw_history_limit, (int, float)) and not isinstance(raw_history_limit, bool)
        else None
    )

    predictions: dict[str, Any] = {}
    weights_by_horizon: dict[str, Any] = {}
    with _apply_candidate_parameters(parameters):
        for hour in HORIZON_HOURS:
            horizon = f"{hour}h"
            weights, diagnostics = aw.adaptive_model_weights(
                names,
                regime,
                hour,
                history,
                dict(actual_by_timestamp),
                history_limit=history_limit,
            )
            weights_by_horizon[horizon] = weights
            price = _ensemble_price(current_price, model_predictions, horizon, weights)
            production_item = production_predictions.get(horizon)
            production_item = production_item if isinstance(production_item, dict) else {}
            half_width = 0.0
            production_q10 = production_item.get("q10_usd")
            production_q90 = production_item.get("q90_usd")
            if production_q10 is not None and production_q90 is not None:
                half_width = max(0.0, (float(production_q90) - float(production_q10)) / 2.0)
            base_half_width = max(half_width, current_price * 0.0005)
            price_value = max(0.01, price)
            q10 = max(0.01, price_value - base_half_width)
            q90 = price_value + base_half_width
            moves: list[int] = []
            for name, item in model_predictions.items():
                if not isinstance(item, dict) or "price_usd" not in item:
                    continue
                if name not in weights or float(weights[name]) <= 0:
                    continue
                item_price = float(item["price_usd"])
                moves.append(1 if item_price > current_price else -1)
            agreement = (
                max(moves.count(1), moves.count(-1), moves.count(0)) / len(moves) if moves else 0.0
            )
            predictions[horizon] = {
                "price_usd": round(price_value, 2),
                "change_pct": round((price_value / current_price - 1.0) * 100.0, 4),
                "q10_usd": round(q10, 2),
                "q50_usd": round(price_value, 2),
                "q90_usd": round(q90, 2),
                "model_agreement": round(agreement, 4),
                "weighting_mode": str(diagnostics.get("mode")),
                "weighting_samples": int(diagnostics.get("sample_count", 0)),
                "interval_source": "production_calibrated_shadow",
            }
    return predictions, weights_by_horizon


def _forecast_sha256(origin_at: str, predictions: Mapping[str, Any]) -> str:
    return _sha256_text(_canonical_json({"origin_at": origin_at, "predictions": predictions}))[:24]


def run_shadow(
    store: ShadowStore,
    production: Mapping[str, Any],
    actual_by_timestamp: Mapping[int, float],
    *,
    configs: Iterable[Mapping[str, Any]] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Persist champion and approved challenger shadow forecasts idempotently.

    The champion shadow row reuses the public production predictions untouched;
    approved challengers get their own reweighted predictions stored separately.
    """
    latest_close_at = str(production["latest_close_at"])
    origin_at = latest_close_at
    latest_close_usd = float(production["latest_close_usd"])
    regime = str(production.get("regime") or "range")
    manifest = production.get("experiment_manifest")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("data_id"), str):
        raise ValueError("production snapshot is missing experiment_manifest.data_id")
    data_lineage_id = str(manifest["data_id"])
    manifest_origin = manifest.get("data", {}).get("latest_close_at")
    if manifest_origin is not None and str(manifest_origin) != origin_at:
        raise ValueError("production snapshot manifest origin does not match latest_close_at")
    model_predictions = production.get("model_predictions")
    if not isinstance(model_predictions, dict):
        raise ValueError("production snapshot is missing model_predictions")
    predictions = production.get("predictions")
    if not isinstance(predictions, dict):
        raise ValueError("production snapshot is missing predictions")
    model_weights = production.get("model_weights")
    if not isinstance(model_weights, dict):
        model_weights = {}

    champion_record, _champion_created = store.register_configuration(
        name=PRODUCTION_CHAMPION_NAME,
        parameters={},
        role="champion",
    )
    champion_configuration_id = str(champion_record["configuration_id"])
    champion_row, champion_persisted = store.record_forecast(
        configuration_id=champion_configuration_id,
        origin_at=origin_at,
        latest_close_at=latest_close_at,
        latest_close_usd=latest_close_usd,
        regime=regime,
        model_predictions=model_predictions,
        model_weights=model_weights,
        predictions=predictions,
        forecast_sha256=_forecast_sha256(origin_at, predictions),
        data_lineage_id=data_lineage_id,
        created_at=generated_at,
    )

    challenger_results: list[dict[str, Any]] = []
    if configs is not None:
        challenger_configs: list[dict[str, Any]] = []
        for item in configs:
            raw_parameters = item.get("parameters")
            challenger_configs.append(
                {
                    "name": str(item.get("name") or "challenger"),
                    "parameters": dict(raw_parameters) if isinstance(raw_parameters, dict) else {},
                }
            )
    else:
        challenger_configs = store.approved_challengers()

    challenger_history: dict[str, list[dict[str, Any]]] = {}
    for config in challenger_configs:
        record, _registered = store.register_configuration(
            name=str(config["name"]),
            parameters=config.get("parameters", {}),
            role="challenger",
            approval_status="approved",
        )
        configuration_id = str(record["configuration_id"])
        if configuration_id == champion_configuration_id:
            raise ValueError(f"challenger configuration {config!r} collides with the champion")
        history = challenger_history.get(configuration_id)
        if history is None:
            history = _history_snapshots(store, configuration_id, exclude_origin_at=origin_at)
            challenger_history[configuration_id] = history
        try:
            challenger_predictions_value, weights = shadow_predictions(
                record["parameters"],
                production,
                actual_by_timestamp,
                history,
            )
            _row, persisted = store.record_forecast(
                configuration_id=configuration_id,
                origin_at=origin_at,
                latest_close_at=latest_close_at,
                latest_close_usd=latest_close_usd,
                regime=regime,
                model_predictions=model_predictions,
                model_weights=weights,
                predictions=challenger_predictions_value,
                forecast_sha256=_forecast_sha256(origin_at, challenger_predictions_value),
                data_lineage_id=data_lineage_id,
                created_at=generated_at,
            )
            challenger_results.append(
                {
                    "name": record["name"],
                    "configuration_id": configuration_id,
                    "approval_status": record["approval_status"],
                    "origin_at": origin_at,
                    "challenger_persisted": persisted,
                    "champion_persisted": champion_persisted,
                    "status": "recorded",
                }
            )
        except (KeyError, TypeError, ValueError) as error:
            store.record_failure(
                stage="shadow_prediction",
                error=error,
                configuration_id=configuration_id,
                origin_at=origin_at,
                observed_at=generated_at,
            )
            challenger_results.append(
                {
                    "name": record["name"],
                    "configuration_id": configuration_id,
                    "approval_status": record["approval_status"],
                    "origin_at": origin_at,
                    "status": "failed",
                    "error_type": type(error).__name__,
                }
            )

    return {
        "schema_version": SHADOW_REPORT_VERSION,
        "generated_at": generated_at or _utc_now_iso(),
        "origin_at": origin_at,
        "shadow_champion": {
            "configuration_id": champion_configuration_id,
            "name": champion_record["name"],
            "persisted": champion_persisted,
            "public_predictions_reused": True,
        },
        "data_lineage": {
            "data_id": data_lineage_id,
            "origin_at": origin_at,
            "identical_to_public_forecast": True,
        },
        "challengers": challenger_results,
        "production_output_changed": False,
        "production_parameters_touched": False,
    }


def mature_shadow_outcomes(
    store: ShadowStore,
    actual_by_timestamp: Mapping[int, float],
    *,
    now: str | None = None,
) -> dict[str, Any]:
    return store.mature_outcomes(actual_by_timestamp, now=now or _utc_now_iso())


def build_shadow_status(
    store: ShadowStore,
    *,
    policy: ShadowPolicy | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build the overall shadow-deployment status report for Actions."""
    active = policy or ShadowPolicy()
    evaluations: list[dict[str, Any]] = []
    for challenger in store.approved_challengers():
        evaluations.append(store.evaluate(challenger["configuration_id"], policy=active))
    champion: dict[str, Any] | None = None
    try:
        champion_config = store.champion_configuration()
        champion = {
            "configuration_id": champion_config["configuration_id"],
            "name": champion_config["name"],
            "parameters": champion_config["parameters"],
            "shadow_forecasts": store.count_forecasts(champion_config["configuration_id"]),
        }
    except KeyError:
        champion = None
    return {
        "schema_version": SHADOW_REPORT_VERSION,
        "generated_at": generated_at or _utc_now_iso(),
        "policy_id": shadow_policy_identity(active),
        "policy": asdict(active),
        "champion": champion,
        "challengers": evaluations,
        "statistics": store.stats(),
    }


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):.{digits}f}"
    return str(value)


def render_summary(status: Mapping[str, Any]) -> str:
    champion = status.get("champion") if isinstance(status.get("champion"), dict) else None
    statistics = status.get("statistics", {})
    if isinstance(statistics, dict):
        shadow_forecasts = int(statistics.get("shadow_forecasts", 0))
        matured_outcomes = int(statistics.get("matured_outcomes", 0))
        per_origin = int(champion.get("shadow_forecasts", 0)) if champion else 0
    else:
        shadow_forecasts = 0
        matured_outcomes = 0
        per_origin = 0

    lines = [
        "# Shadow deployment",
        "",
        f"- Policy: `{status.get('policy_id')}`",
        f"- Champion: **{champion['name']}** (`{champion['configuration_id']}`)"
        if champion
        else "- Champion: **none registered**",
        f"- Persisted shadow forecasts: **{shadow_forecasts}**",
        f"- Matured outcomes: **{matured_outcomes}** (across **{per_origin}** champion origins)",
        "",
        "## Challengers",
        "",
    ]
    evaluations = status.get("challengers")
    if isinstance(evaluations, list) and evaluations:
        lines.extend(
            [
                "| Challenger | Configuration | Live samples | Window (days) | "
                "Champion MAE | Challenger MAE | Persistence MAE | Promotion |",
                "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        for evaluation in evaluations:
            challenger = evaluation.get("challenger", {})
            maturity = evaluation.get("maturity", {})
            metrics = evaluation.get("metrics", {})
            promotion = evaluation.get("promotion", {})
            challenger_metric = metrics.get("challenger", {}) if isinstance(metrics, dict) else {}
            champion_metric = metrics.get("champion", {}) if isinstance(metrics, dict) else {}
            persistence_metric = metrics.get("persistence", {}) if isinstance(metrics, dict) else {}
            lines.append(
                f"| **{challenger.get('name', 'n/a')}** | "
                f"`{challenger.get('configuration_id', 'n/a')}` | "
                f"{_fmt(maturity.get('live_samples'), 0)} | "
                f"{_fmt(maturity.get('observation_window_days'), 1)} | "
                f"{_fmt(champion_metric.get('mae_pct'))}% | "
                f"{_fmt(challenger_metric.get('mae_pct'))}% | "
                f"{_fmt(persistence_metric.get('mae_pct'))}% | "
                f"**{promotion.get('decision', 'blocked').upper()}** |"
            )
    else:
        lines.append("No approved challengers are currently running in shadow mode.")
    lines.extend(
        [
            "",
            "Shadow forecasts never change the public forecast output. Promotion "
            "stays blocked until configured live-observation requirements are met.",
            "",
        ]
    )
    return "\n".join(lines)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(data: Any, stream: TextIO = sys.stdout) -> None:
    json.dump(data, stream, indent=2, sort_keys=True)
    stream.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run and evaluate shadow deployments for research challengers"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init")
    subparsers.add_parser("verify")
    subparsers.add_parser("stats")

    approve = subparsers.add_parser("approve")
    approve.add_argument("--name", required=True)
    approve.add_argument("--parameters", type=Path, help="JSON object of challenger parameters")
    approve.add_argument("--role", choices=VALID_ROLES, default="challenger")
    approve.add_argument("--approval-status", choices=VALID_APPROVAL_STATUSES, default="approved")

    record_parser = subparsers.add_parser("record")
    record_parser.add_argument(
        "--production", type=Path, required=True, help="production snapshot JSON"
    )
    record_parser.add_argument("--actuals", type=Path, default=DEFAULT_ACTUALS_PATH)
    record_parser.add_argument(
        "--configs", type=Path, help="list of {name, parameters} configurations"
    )

    mature = subparsers.add_parser("mature")
    mature.add_argument("--actuals", type=Path, required=True)

    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("--configuration-id", required=True)
    evaluate.add_argument(
        "--minimum-samples", type=int, default=ShadowPolicy().minimum_live_samples
    )
    evaluate.add_argument(
        "--observation-window-days", type=int, default=ShadowPolicy().observation_window_days
    )

    report = subparsers.add_parser("report")
    report.add_argument("--minimum-samples", type=int, default=ShadowPolicy().minimum_live_samples)
    report.add_argument(
        "--observation-window-days", type=int, default=ShadowPolicy().observation_window_days
    )
    report.add_argument("--out", type=Path, default=DEFAULT_REPORT_PATH)
    report.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY_PATH)

    args = parser.parse_args()
    store = ShadowStore(args.db)

    if args.command == "init":
        _write_json(store.verify())
    elif args.command == "verify":
        _write_json(store.verify())
    elif args.command == "stats":
        _write_json(store.stats())
    elif args.command == "approve":
        parameters = _read_json(args.parameters) if args.parameters else {}
        if not isinstance(parameters, dict):
            raise ValueError("--parameters must contain a JSON object")
        record, created = store.register_configuration(
            name=args.name,
            parameters=parameters,
            role=args.role,
            approval_status=args.approval_status,
        )
        _write_json({"created": created, "configuration": record})
    elif args.command == "record":
        production = _read_json(args.production)
        if not isinstance(production, dict):
            raise ValueError("--production must contain a JSON object")
        actuals = _read_json(args.actuals) if args.actuals.exists() else {}
        if not isinstance(actuals, dict):
            raise ValueError("--actuals must contain a JSON object")
        actual_by_timestamp = {int(k): float(v) for k, v in actuals.items() if float(v) > 0}
        configs = None
        if args.configs is not None:
            configs = _read_json(args.configs)
            if not isinstance(configs, list):
                raise ValueError("--configs must contain a JSON list")
        _write_json(
            run_shadow(
                store,
                production,
                actual_by_timestamp,
                configs=configs,
            )
        )
    elif args.command == "mature":
        actuals = _read_json(args.actuals)
        if not isinstance(actuals, dict):
            raise ValueError("--actuals must contain a JSON object")
        actual_by_timestamp = {int(k): float(v) for k, v in actuals.items() if float(v) > 0}
        _write_json(mature_shadow_outcomes(store, actual_by_timestamp))
    elif args.command == "evaluate":
        policy = ShadowPolicy(
            minimum_live_samples=args.minimum_samples,
            observation_window_days=args.observation_window_days,
        )
        _write_json(store.evaluate(args.configuration_id, policy=policy))
    elif args.command == "report":
        policy = ShadowPolicy(
            minimum_live_samples=args.minimum_samples,
            observation_window_days=args.observation_window_days,
        )
        status = build_shadow_status(store, policy=policy)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        args.summary.write_text(render_summary(status), encoding="utf-8")
        print(args.summary.read_text(encoding="utf-8"))
        print(f"Saved {args.out} and {args.summary}")


if __name__ == "__main__":
    main()
