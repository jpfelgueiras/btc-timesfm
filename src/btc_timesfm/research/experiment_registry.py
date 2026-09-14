#!/usr/bin/env python3
"""Durable experiment registry and longitudinal leaderboard.

Every automated research run can register an experiment once per ``run_id``.
Retries are idempotent: the first write wins and duplicates never grow the
registry. Production champions are flagged distinctly from research leaders,
and every registered decision is appended to an audit table so historical
decisions stay reproducible.

The registry follows the durable-SQLite conventions of
``btc_timesfm.history.history_store``: ``PRAGMA user_version`` schema versioning,
a `metadata`/`schema_migrations`` audit trail, byte-for-byte rollback backups on
migration, and a bounded append-only schema keyed by ``run_id``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, TextIO


CURRENT_SCHEMA_VERSION = 1
SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
LEADERBOARD_VERSION = 1
DEFAULT_DB_PATH = Path(".state/experiment_registry.sqlite")
DEFAULT_LEADERBOARD_PATH = Path("experiment_leaderboard.json")
DEFAULT_SUMMARY_PATH = Path("experiment_leaderboard.md")
VALID_DECISIONS = ("champion", "candidate", "inconclusive", "rejected")
CHAMPION_DECISION = "champion"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _compact_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _loads_optional(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):.{digits}f}"
    return str(value)


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


def _migration_1_initial_experiment_registry(connection: sqlite3.Connection) -> None:
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
        CREATE TABLE IF NOT EXISTS experiments (
            run_id TEXT PRIMARY KEY,
            run_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            configuration_id TEXT,
            data_id TEXT,
            hypothesis TEXT,
            feature_set_version TEXT,
            model_set TEXT,
            evaluation_window TEXT,
            metrics TEXT NOT NULL,
            statistical_evidence TEXT,
            decision TEXT NOT NULL DEFAULT 'candidate'
                CHECK (decision IN ('champion', 'candidate', 'inconclusive', 'rejected')),
            parent_run_id TEXT,
            git_sha TEXT,
            report_path TEXT,
            updated_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS experiment_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            decision TEXT NOT NULL
                CHECK (decision IN ('champion', 'candidate', 'inconclusive', 'rejected')),
            decided_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES experiments(run_id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_experiments_decision_created
            ON experiments(decision, created_at)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_experiments_configuration_created
            ON experiments(configuration_id, created_at)
        """
    )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "initial_experiment_registry", _migration_1_initial_experiment_registry),
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

    required_tables = {"metadata", "schema_migrations", "experiments", "experiment_decisions"}
    missing = sorted(required_tables - tables)
    if missing:
        raise RuntimeError(f"Experiment registry is missing required tables: {', '.join(missing)}")
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
) -> dict[str, Any]:
    """Create or upgrade the registry database with rollback protection."""
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = _validate_registry(migrations, target_version)
    source_version = _read_schema_version(db_path)
    if source_version > target_version:
        raise RuntimeError(
            f"Experiment registry schema {source_version} is newer than supported {target_version}"
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


class ExperimentRegistry:
    """SQLite-backed append-safe registry of forecasting experiments."""

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

    def register_experiment(
        self,
        *,
        run_id: str,
        run_type: str,
        created_at: str | None = None,
        configuration_id: str | None = None,
        data_id: str | None = None,
        hypothesis: str | None = None,
        feature_set_version: str | None = None,
        model_set: list[str] | None = None,
        evaluation_window: dict[str, Any] | list[Any] | None = None,
        metrics: dict[str, Any] | None = None,
        statistical_evidence: dict[str, Any] | None = None,
        decision: str = "candidate",
        parent_run_id: str | None = None,
        git_sha: str | None = None,
        report_path: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Register one experiment, first-write-wins on ``run_id``.

        Registering an already-known ``run_id`` (a retried or duplicated run)
        returns the original record unchanged with ``created=False``.
        """
        if not run_id:
            raise ValueError("run_id must be a non-empty string")
        if not run_type:
            raise ValueError("run_type must be a non-empty string")
        if decision not in VALID_DECISIONS:
            raise ValueError(f"invalid decision {decision!r}; expected one of {VALID_DECISIONS}")
        if metrics is not None and not isinstance(metrics, dict):
            raise ValueError("metrics must be a JSON object")
        if statistical_evidence is not None and not isinstance(statistical_evidence, dict):
            raise ValueError("statistical_evidence must be a JSON object")

        metrics_json = _compact_json(metrics or {})
        evidence_json = _compact_json(statistical_evidence or {})
        model_set_json = _compact_json(list(model_set) if model_set is not None else None)
        evaluation_window_json = _compact_json(evaluation_window)
        now = _utc_now_iso()
        created = created_at or now

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO experiments(
                    run_id, run_type, created_at, configuration_id, data_id,
                    hypothesis, feature_set_version, model_set, evaluation_window,
                    metrics, statistical_evidence, decision, parent_run_id,
                    git_sha, report_path, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    run_type,
                    created,
                    configuration_id,
                    data_id,
                    hypothesis,
                    feature_set_version,
                    model_set_json,
                    evaluation_window_json,
                    metrics_json,
                    evidence_json,
                    decision,
                    parent_run_id,
                    git_sha,
                    report_path,
                    now,
                ),
            )
            created_flag = cursor.rowcount > 0
            row = connection.execute(
                "SELECT * FROM experiments WHERE run_id = ?", (run_id,)
            ).fetchone()
        return self._decode_row(row), created_flag

    def register_from_manifest(
        self,
        manifest: dict[str, Any],
        *,
        metrics: dict[str, Any] | None = None,
        statistical_evidence: dict[str, Any] | None = None,
        decision: str = "candidate",
        hypothesis: str | None = None,
        feature_set_version: str | None = None,
        model_set: list[str] | None = None,
        evaluation_window: dict[str, Any] | list[Any] | None = None,
        parent_run_id: str | None = None,
        report_path: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Register from a ``build_experiment_manifest`` dict produced by
        ``btc_timesfm.forecasting.experiment_manifest``."""
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be a JSON object")
        run_id = manifest.get("run_id")
        if not run_id:
            raise ValueError("manifest is missing run_id")
        code = manifest.get("code")
        configuration = manifest.get("configuration")
        git_sha = (
            str(code.get("git_sha")) if isinstance(code, dict) and code.get("git_sha") else None
        )
        if feature_set_version is None and isinstance(configuration, dict):
            feature_set_version = configuration.get("feature_set_version")
        if model_set is None and isinstance(configuration, dict):
            forecast = configuration.get("forecast", {})
            if isinstance(forecast, dict) and isinstance(forecast.get("model_names"), list):
                model_set = [str(name) for name in forecast["model_names"]]
        return self.register_experiment(
            run_id=str(run_id),
            run_type=str(manifest.get("run_type") or "research"),
            created_at=(
                str(manifest["created_at"]) if manifest.get("created_at") is not None else None
            ),
            configuration_id=(
                str(manifest["configuration_id"])
                if manifest.get("configuration_id") is not None
                else None
            ),
            data_id=str(manifest["data_id"]) if manifest.get("data_id") is not None else None,
            hypothesis=hypothesis,
            feature_set_version=feature_set_version,
            model_set=model_set,
            evaluation_window=evaluation_window,
            metrics=metrics,
            statistical_evidence=statistical_evidence,
            decision=decision,
            parent_run_id=parent_run_id,
            git_sha=git_sha,
            report_path=report_path,
        )

    @staticmethod
    def _decode_row(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise KeyError("experiment not found")
        record = dict(row)
        for key in ("model_set", "evaluation_window", "metrics", "statistical_evidence"):
            record[key] = _loads_optional(record.get(key))
        return record

    def get(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM experiments WHERE run_id = ?", (run_id,)
            ).fetchone()
        return self._decode_row(row)

    def update_decision(self, run_id: str, decision: str) -> dict[str, Any]:
        """Record the final decision; every change is appended to the audit trail."""
        if decision not in VALID_DECISIONS:
            raise ValueError(f"invalid decision {decision!r}; expected one of {VALID_DECISIONS}")
        now = _utc_now_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE experiments SET decision = ?, updated_at = ? WHERE run_id = ?",
                (decision, now, run_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"unknown run_id {run_id!r}")
            connection.execute(
                """
                INSERT INTO experiment_decisions(run_id, decision, decided_at)
                VALUES(?, ?, ?)
                """,
                (run_id, decision, now),
            )
            row = connection.execute(
                "SELECT * FROM experiments WHERE run_id = ?", (run_id,)
            ).fetchone()
        return self._decode_row(row)

    def leaderboard(
        self,
        *,
        horizon: str | None = None,
        regime: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """Build the longitudinal leaderboard separating research and champion.

        ``horizon``/``regime`` filter the stored per-horizon/per-regime metrics;
        ``status`` filters on the recorded decision. Candidates are sorted by the
        selected metric's ``mae_pct`` (ascending); the production champion is the
        most recently created ``champion`` decision and is flagged ``is_champion``.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM experiments ORDER BY created_at, run_id"
            ).fetchall()

        entries: list[dict[str, Any]] = []
        for row in rows:
            record = self._decode_row(row)
            if status is not None and record["decision"] != status:
                continue
            metric = _metric_for(record["metrics"], horizon, regime)
            if horizon is not None or regime is not None:
                if not metric:
                    continue
            is_champion = record["decision"] == CHAMPION_DECISION
            entries.append(
                {
                    "run_id": record["run_id"],
                    "run_type": record["run_type"],
                    "created_at": record["created_at"],
                    "configuration_id": record["configuration_id"],
                    "data_id": record["data_id"],
                    "hypothesis": record["hypothesis"],
                    "feature_set_version": record["feature_set_version"],
                    "model_set": record["model_set"],
                    "evaluation_window": record["evaluation_window"],
                    "decision": record["decision"],
                    "role": "champion" if is_champion else "research",
                    "is_champion": is_champion,
                    "parent_run_id": record["parent_run_id"],
                    "git_sha": record["git_sha"],
                    "report_path": record["report_path"],
                    "metric": metric,
                    "metric_summary": _metric_summary(metric),
                    "statistical_evidence": record["statistical_evidence"],
                }
            )

        champions = [entry for entry in entries if entry["is_champion"]]
        champion = max(champions, key=lambda item: item["created_at"]) if champions else None
        candidates = [entry for entry in entries if not entry["is_champion"]]
        candidates.sort(key=_candidate_sort_key)

        ordered_entries = ([champion] if champion is not None else []) + candidates
        audit_filters = {
            "horizon": horizon,
            "regime": regime,
            "status": status,
        }
        audit_payload = {
            "schema_version": LEADERBOARD_VERSION,
            "filters": audit_filters,
            "entries": ordered_entries,
        }
        audit_sha256 = hashlib.sha256(_canonical_json(audit_payload).encode("utf-8")).hexdigest()
        return {
            "schema_version": LEADERBOARD_VERSION,
            "generated_at": _utc_now_iso(),
            "filters": audit_filters,
            "champion": champion,
            "candidates": candidates,
            "entries": ordered_entries,
            "statistics": {
                "registered_experiments": len(rows),
                "matching_experiments": len(entries),
                "champion": len(champions),
                "candidates": len(candidates),
            },
            "audit": {"sha256": audit_sha256},
        }

    def stats(self) -> dict[str, Any]:
        with self._connect() as connection:
            total = int(connection.execute("SELECT COUNT(*) FROM experiments").fetchone()[0])
            by_decision: dict[str, int] = {}
            for row in connection.execute(
                "SELECT decision, COUNT(*) AS n FROM experiments GROUP BY decision"
            ).fetchall():
                by_decision[str(row["decision"])] = int(row["n"])
            by_run_type: dict[str, int] = {}
            for row in connection.execute(
                "SELECT run_type, COUNT(*) AS n FROM experiments GROUP BY run_type"
            ).fetchall():
                by_run_type[str(row["run_type"])] = int(row["n"])
            decisions = int(
                connection.execute("SELECT COUNT(*) FROM experiment_decisions").fetchone()[0]
            )
            first_last = connection.execute(
                "SELECT MIN(created_at), MAX(created_at) FROM experiments"
            ).fetchone()
            latest_champion = connection.execute(
                "SELECT run_id FROM experiments WHERE decision = 'champion' "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        diagnostics = schema_diagnostics(self.path)
        return {
            "schema_version": diagnostics["schema_version"],
            "supported_schema_version": diagnostics["supported_schema_version"],
            "applied_migrations": diagnostics["applied_migrations"],
            "experiments": total,
            "by_decision": by_decision,
            "by_run_type": by_run_type,
            "decision_updates": decisions,
            "latest_champion_run_id": (
                str(latest_champion["run_id"]) if latest_champion is not None else None
            ),
            "first_experiment_at": first_last[0],
            "latest_experiment_at": first_last[1],
            "database_bytes": self.path.stat().st_size if self.path.exists() else 0,
        }

    def verify(self) -> dict[str, Any]:
        return validate_database(self.path)


def _metric_for(metrics: Any, horizon: str | None, regime: str | None) -> dict[str, Any]:
    """Resolve the leaderboard metric block for the requested filters."""
    if not isinstance(metrics, dict):
        return {}
    if horizon is not None and regime is not None:
        block = metrics.get("by_regime", {}).get(regime, {}).get(horizon)
        return dict(block) if isinstance(block, dict) else {}
    if horizon is not None:
        block = metrics.get("by_horizon", {}).get(horizon)
        return dict(block) if isinstance(block, dict) else {}
    if regime is not None:
        by_regime = metrics.get("by_regime", {})
        regime_metrics = by_regime.get(regime)
        if not isinstance(regime_metrics, dict):
            return {}
        by_horizon: dict[str, Any] = {}
        mae_values: list[float] = []
        for label, block in regime_metrics.items():
            if not isinstance(block, dict):
                continue
            by_horizon[label] = dict(block)
            value = block.get("mae_pct")
            if value is not None:
                try:
                    mae_values.append(float(value))
                except (TypeError, ValueError):
                    pass
        return {
            "samples": _mean(
                [
                    float(block["samples"])
                    for block in by_horizon.values()
                    if isinstance(block.get("samples"), (int, float))
                ]
            ),
            "mae_pct": _mean(mae_values),
            "mean_signed_error_pct": _mean(
                [
                    float(block["mean_signed_error_pct"])
                    for block in by_horizon.values()
                    if block.get("mean_signed_error_pct") is not None
                ]
            ),
            "direction_accuracy": _mean(
                [
                    float(block["direction_accuracy"])
                    for block in by_horizon.values()
                    if block.get("direction_accuracy") is not None
                ]
            ),
            "by_horizon": by_horizon,
        }
    return {
        "samples": metrics.get("samples"),
        "mae_pct": metrics.get("objective_mae_pct"),
        "mean_signed_error_pct": metrics.get("mean_signed_error_pct"),
        "direction_accuracy": metrics.get("mean_direction_accuracy"),
    }


def _metric_summary(metric: dict[str, Any]) -> dict[str, Any]:
    if not metric:
        return {
            "available": False,
            "samples": None,
            "mae_pct": None,
            "mean_signed_error_pct": None,
            "direction_accuracy": None,
        }
    return {
        "available": True,
        "samples": metric.get("samples"),
        "mae_pct": metric.get("mae_pct"),
        "mean_signed_error_pct": metric.get("mean_signed_error_pct"),
        "direction_accuracy": metric.get("direction_accuracy"),
    }


def _candidate_sort_key(entry: dict[str, Any]) -> tuple[int, float, str]:
    metric = entry.get("metric")
    value: float | None = None
    if isinstance(metric, dict):
        raw = metric.get("mae_pct")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            value = float(raw)
    if value is not None:
        return (0, value, str(entry["created_at"]))
    return (1, float("inf"), str(entry["created_at"]))


def build_leaderboard(
    db: Path | str | ExperimentRegistry,
    *,
    horizon: str | None = None,
    regime: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Build a leaderboard from a registry database path or instance."""
    registry = db if isinstance(db, ExperimentRegistry) else ExperimentRegistry(db)
    return registry.leaderboard(horizon=horizon, regime=regime, status=status)


def render_json(leaderboard: dict[str, Any]) -> str:
    return json.dumps(leaderboard, indent=2, sort_keys=True) + "\n"


def _metric_label(horizon: str | None, regime: str | None) -> str:
    if horizon is not None:
        return f"{horizon} MAE"
    if regime is not None:
        return f"{regime} MAE (mean)"
    return "Objective MAE"


def render_markdown(leaderboard: dict[str, Any]) -> str:
    filters = leaderboard["filters"]
    champion = leaderboard["champion"]
    candidates = leaderboard["candidates"]
    metric_label = _metric_label(filters.get("horizon"), filters.get("regime"))
    lines = [
        "# Experiment leaderboard",
        "",
        f"- Filters: horizon=`{filters.get('horizon') or 'all'}`, "
        f"regime=`{filters.get('regime') or 'all'}`, "
        f"status=`{filters.get('status') or 'all'}`",
        f"- Registered experiments: **{leaderboard['statistics']['registered_experiments']}** "
        f"(matching **{leaderboard['statistics']['matching_experiments']}**)",
        f"- Audit `sha256`: `{leaderboard['audit']['sha256']}`",
        "",
    ]
    table_header = (
        "| Role | Decision | Run ID | Configuration | Created | "
        f"{metric_label} (%) | Direction | Samples |"
    )
    table_separator = "| --- | --- | --- | --- | --- | ---: | ---: | ---: |"
    lines.extend(["## Champion", "", table_header, table_separator])
    if champion is not None and isinstance(champion, dict):
        metric = champion["metric"]
        lines.append(
            f"| **CHAMPION** | {champion['decision']} | `{champion['run_id']}` | "
            f"`{champion['configuration_id']}` | {champion['created_at']} | "
            f"{_fmt(metric.get('mae_pct'))} | "
            f"{_fmt(metric.get('direction_accuracy'))} | {_fmt(metric.get('samples'), 0)} |"
        )
    else:
        lines.append("| No champion registered. | | | | | | | |")
    lines.extend(["", "## Research candidates", "", table_header, table_separator])
    if candidates:
        for rank, entry in enumerate(candidates, start=1):
            metric = entry["metric"]
            lines.append(
                f"| {rank} | {entry['decision']} | `{entry['run_id']}` | "
                f"`{entry['configuration_id']}` | {entry['created_at']} | "
                f"{_fmt(metric.get('mae_pct'))} | "
                f"{_fmt(metric.get('direction_accuracy'))} | "
                f"{_fmt(metric.get('samples'), 0)} |"
            )
    else:
        lines.append("| No research candidates in this view. | | | | | | | |")
    lines.extend(
        [
            "",
            "The production champion is the most recent registered `champion` decision; "
            "research candidates are ordered by the selected metric.",
            "",
        ]
    )
    return "\n".join(lines)


def _read_json(path: Path) -> Any:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload


def _write_json(data: Any, stream: TextIO = sys.stdout) -> None:
    json.dump(data, stream, indent=2, sort_keys=True)
    stream.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Register experiments and render the longitudinal leaderboard"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init")
    subparsers.add_parser("verify")
    subparsers.add_parser("stats")

    register = subparsers.add_parser("register")
    register.add_argument("--manifest", type=Path, required=True)
    register.add_argument("--metrics", type=Path)
    register.add_argument("--statistical-evidence", type=Path)
    register.add_argument(
        "--decision",
        choices=VALID_DECISIONS,
        default="candidate",
    )
    register.add_argument("--hypothesis")
    register.add_argument("--feature-set-version")
    register.add_argument("--model-set", help="comma-separated model names")
    register.add_argument("--evaluation-window", type=Path)
    register.add_argument("--parent-run-id")
    register.add_argument("--report-path")

    leaderboard = subparsers.add_parser("leaderboard")
    leaderboard.add_argument("--horizon")
    leaderboard.add_argument("--regime")
    leaderboard.add_argument("--status", choices=VALID_DECISIONS)
    leaderboard.add_argument("--out", type=Path, default=DEFAULT_LEADERBOARD_PATH)
    leaderboard.add_argument("--md", type=Path, default=DEFAULT_SUMMARY_PATH)

    args = parser.parse_args()
    registry = ExperimentRegistry(args.db)

    if args.command == "init":
        _write_json(registry.stats())
    elif args.command == "verify":
        _write_json(registry.verify())
    elif args.command == "stats":
        _write_json(registry.stats())
    elif args.command == "register":
        manifest = _read_json(args.manifest)
        if not isinstance(manifest, dict):
            raise ValueError("--manifest must contain a JSON object")
        metrics = _read_json(args.metrics) if args.metrics else None
        if metrics is not None and not isinstance(metrics, dict):
            raise ValueError("--metrics must contain a JSON object")
        evidence = _read_json(args.statistical_evidence) if args.statistical_evidence else None
        if evidence is not None and not isinstance(evidence, dict):
            raise ValueError("--statistical-evidence must contain a JSON object")
        evaluation_window = _read_json(args.evaluation_window) if args.evaluation_window else None
        model_set = (
            [name.strip() for name in args.model_set.split(",") if name.strip()]
            if args.model_set
            else None
        )
        record, created = registry.register_from_manifest(
            manifest,
            metrics=metrics,
            statistical_evidence=evidence,
            decision=args.decision,
            hypothesis=args.hypothesis,
            feature_set_version=args.feature_set_version,
            model_set=model_set,
            evaluation_window=evaluation_window,
            parent_run_id=args.parent_run_id,
            report_path=args.report_path,
        )
        _write_json({"created": created, "record": record})
    elif args.command == "leaderboard":
        board = build_leaderboard(
            registry, horizon=args.horizon, regime=args.regime, status=args.status
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(render_json(board), encoding="utf-8")
        args.md.write_text(render_markdown(board), encoding="utf-8")
        _write_json({"json": str(args.out), "markdown": str(args.md), **board["statistics"]})


if __name__ == "__main__":
    main()
