#!/usr/bin/env python3
"""Audit and safely repair the durable forecast-history SQLite database.

The audit is intentionally conservative:
- destructive repairs are never performed;
- every writable repair is deterministic from existing immutable fields or from
  an explicitly supplied exact-target price map;
- repair mode creates a byte-for-byte backup before changing the database;
- dry-run and repair modes emit the same machine-readable report shape.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from history_migrations import CURRENT_SCHEMA_VERSION


AUDIT_REPORT_VERSION = 1
DEFAULT_DB_PATH = Path(".state/forecast_history.sqlite")
DEFAULT_GRACE_MINUTES = 15
ENSEMBLE_MODEL = "ensemble"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _direction(value: float, epsilon: float = 1e-9) -> int:
    return 1 if value > epsilon else -1 if value < -epsilon else 0


def _is_finite_number(value: Any, *, positive: bool = False) -> bool:
    if value is None:
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(number):
        return False
    return number > 0 if positive else True


def _close_enough(left: Any, right: Any, tolerance: float = 1e-8) -> bool:
    if left is None or right is None:
        return left is right
    try:
        a = float(left)
        b = float(right)
    except (TypeError, ValueError):
        return False
    scale = max(1.0, abs(a), abs(b))
    return abs(a - b) <= tolerance * scale


def _rows_to_examples(rows: Iterable[sqlite3.Row], limit: int = 5) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    for row in rows:
        examples.append(dict(row))
        if len(examples) >= limit:
            break
    return examples


@dataclass
class Issue:
    code: str
    severity: str
    message: str
    count: int = 1
    repairable: bool = False
    proposed_action: str | None = None
    examples: list[dict[str, Any]] | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "count": int(self.count),
            "repairable": bool(self.repairable),
        }
        if self.proposed_action:
            result["proposed_action"] = self.proposed_action
        if self.examples:
            result["examples"] = self.examples
        return result


def load_actuals(path: Path | str | None) -> dict[int, float]:
    """Load an explicit exact-target price map from JSON."""
    if path is None:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    result: dict[int, float] = {}

    def add(key: Any, value: Any) -> None:
        if not _is_finite_number(value, positive=True):
            raise ValueError(f"Invalid positive price for {key!r}: {value!r}")
        if isinstance(key, (int, float)) or (
            isinstance(key, str) and key.strip().isdigit()
        ):
            timestamp = int(float(key))
        else:
            timestamp = int(_parse_timestamp(str(key)).timestamp())
        result[timestamp] = float(value)

    if isinstance(payload, dict):
        for key, value in payload.items():
            add(key, value)
    elif isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                raise ValueError("Actuals list entries must be JSON objects")
            if "target_at" not in item or "actual_target_price_usd" not in item:
                raise ValueError(
                    "Actuals entries require target_at and actual_target_price_usd"
                )
            add(item["target_at"], item["actual_target_price_usd"])
    else:
        raise ValueError("Actuals JSON must be an object or list")
    return result


def _backup_path(path: Path, now: datetime) -> Path:
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return path.with_name(f"{path.name}.pre-repair-{stamp}.bak")


def _base_report(path: Path, mode: str, now: datetime) -> dict[str, Any]:
    return {
        "report_version": AUDIT_REPORT_VERSION,
        "database": str(path),
        "mode": mode,
        "checked_at": _iso(now),
        "supported_schema_version": CURRENT_SCHEMA_VERSION,
        "healthy": False,
        "issues": [],
        "repairs": {
            "backup": None,
            "proposed": [],
            "applied": [],
        },
        "summary": {
            "errors": 0,
            "warnings": 0,
            "info": 0,
            "repairable_issues": 0,
            "proposed_actions": 0,
            "applied_actions": 0,
        },
    }


def _finalize_report(report: dict[str, Any]) -> dict[str, Any]:
    issues = report["issues"]
    summary = report["summary"]
    summary["errors"] = sum(1 for issue in issues if issue["severity"] == "error")
    summary["warnings"] = sum(
        1 for issue in issues if issue["severity"] == "warning"
    )
    summary["info"] = sum(1 for issue in issues if issue["severity"] == "info")
    summary["repairable_issues"] = sum(
        1 for issue in issues if issue.get("repairable")
    )
    summary["proposed_actions"] = len(report["repairs"]["proposed"])
    summary["applied_actions"] = len(report["repairs"]["applied"])
    report["healthy"] = summary["errors"] == 0
    return report


def _issue(report: dict[str, Any], item: Issue) -> None:
    report["issues"].append(item.as_dict())


def _propose(report: dict[str, Any], action: dict[str, Any]) -> None:
    report["repairs"]["proposed"].append(action)


def _audit_connection(
    connection: sqlite3.Connection,
    report: dict[str, Any],
    *,
    now: datetime,
    maturity_grace_minutes: int,
    actual_by_timestamp: dict[int, float],
) -> None:
    connection.row_factory = sqlite3.Row

    integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
    integrity_messages = [str(row[0]) for row in integrity_rows]
    report["sqlite_integrity"] = integrity_messages
    if integrity_messages != ["ok"]:
        _issue(
            report,
            Issue(
                "sqlite_integrity",
                "error",
                "SQLite integrity_check reported database corruption.",
                count=len(integrity_messages),
                examples=[
                    {"message": message} for message in integrity_messages[:5]
                ],
            ),
        )

    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    required_tables = {
        "metadata",
        "forecast_origins",
        "forecast_predictions",
        "schema_migrations",
    }
    missing_tables = sorted(required_tables - tables)
    report["tables"] = sorted(tables)
    if missing_tables:
        _issue(
            report,
            Issue(
                "missing_tables",
                "error",
                "History database is missing required tables.",
                count=len(missing_tables),
                examples=[{"table": table} for table in missing_tables],
            ),
        )
        return

    required_columns = {
        "forecast_origins": {
            "origin_at",
            "generated_at",
            "source_price_usd",
            "market_features_json",
            "first_seen_at",
            "last_seen_at",
        },
        "forecast_predictions": {
            "origin_at",
            "model_name",
            "horizon_hours",
            "target_at",
            "predicted_price_usd",
            "predicted_change_pct",
            "q10_usd",
            "q90_usd",
            "actual_target_price_usd",
            "absolute_error_usd",
            "absolute_error_pct",
            "signed_error_pct",
            "actual_change_pct",
            "direction_correct",
            "within_q10_q90",
            "matured_at",
        },
    }
    missing_columns: list[dict[str, Any]] = []
    for table, expected in required_columns.items():
        existing = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        missing = sorted(expected - existing)
        if missing:
            missing_columns.append({"table": table, "columns": missing})
    if missing_columns:
        _issue(
            report,
            Issue(
                "missing_columns",
                "error",
                "History database tables are missing required columns.",
                count=sum(len(item["columns"]) for item in missing_columns),
                examples=missing_columns[:5],
            ),
        )
        return

    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    report["schema_version"] = user_version
    if user_version != CURRENT_SCHEMA_VERSION:
        _issue(
            report,
            Issue(
                "schema_version",
                "error",
                f"Schema version {user_version} does not match supported version "
                f"{CURRENT_SCHEMA_VERSION}. Run the normal migration command first.",
            ),
        )

    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    report["foreign_key_violations"] = len(foreign_keys)
    if foreign_keys:
        _issue(
            report,
            Issue(
                "foreign_key_violation",
                "error",
                "Foreign-key violations exist. Automatic repair will not delete rows.",
                count=len(foreign_keys),
                examples=_rows_to_examples(foreign_keys),
            ),
        )

    duplicate_origins = connection.execute(
        """
        SELECT origin_at, COUNT(*) AS duplicate_count
        FROM forecast_origins
        GROUP BY origin_at
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    if duplicate_origins:
        _issue(
            report,
            Issue(
                "duplicate_origins",
                "error",
                "Duplicate logical forecast origins were found.",
                count=len(duplicate_origins),
                examples=_rows_to_examples(duplicate_origins),
            ),
        )

    duplicate_predictions = connection.execute(
        """
        SELECT origin_at, model_name, horizon_hours, COUNT(*) AS duplicate_count
        FROM forecast_predictions
        GROUP BY origin_at, model_name, horizon_hours
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    if duplicate_predictions:
        _issue(
            report,
            Issue(
                "duplicate_predictions",
                "error",
                "Duplicate logical prediction keys were found.",
                count=len(duplicate_predictions),
                examples=_rows_to_examples(duplicate_predictions),
            ),
        )

    orphan_predictions = connection.execute(
        """
        SELECT p.origin_at, p.model_name, p.horizon_hours
        FROM forecast_predictions AS p
        LEFT JOIN forecast_origins AS o USING(origin_at)
        WHERE o.origin_at IS NULL
        ORDER BY p.origin_at, p.horizon_hours, p.model_name
        """
    ).fetchall()
    if orphan_predictions:
        _issue(
            report,
            Issue(
                "orphan_prediction_rows",
                "error",
                "Prediction rows reference a missing forecast origin. They are reported "
                "but never deleted automatically.",
                count=len(orphan_predictions),
                examples=_rows_to_examples(orphan_predictions),
            ),
        )

    orphan_model_groups = connection.execute(
        """
        SELECT p.origin_at, p.horizon_hours, COUNT(*) AS model_rows
        FROM forecast_predictions AS p
        WHERE p.model_name <> ?
          AND NOT EXISTS (
              SELECT 1
              FROM forecast_predictions AS e
              WHERE e.origin_at = p.origin_at
                AND e.horizon_hours = p.horizon_hours
                AND e.model_name = ?
          )
        GROUP BY p.origin_at, p.horizon_hours
        ORDER BY p.origin_at, p.horizon_hours
        """,
        (ENSEMBLE_MODEL, ENSEMBLE_MODEL),
    ).fetchall()
    if orphan_model_groups:
        _issue(
            report,
            Issue(
                "orphan_model_groups",
                "error",
                "Underlying model rows exist without the corresponding ensemble row.",
                count=len(orphan_model_groups),
                examples=_rows_to_examples(orphan_model_groups),
            ),
        )

    invalid_origins: list[dict[str, Any]] = []
    origins = connection.execute(
        """
        SELECT origin_at, generated_at, source_price_usd, market_features_json,
               first_seen_at, last_seen_at
        FROM forecast_origins
        ORDER BY origin_at
        """
    ).fetchall()
    for row in origins:
        problems: list[str] = []
        for column in ("origin_at", "generated_at", "first_seen_at", "last_seen_at"):
            try:
                _parse_timestamp(row[column])
            except (TypeError, ValueError):
                problems.append(f"invalid_{column}")
        if not _is_finite_number(row["source_price_usd"], positive=True):
            problems.append("invalid_source_price_usd")
        try:
            features = json.loads(row["market_features_json"])
            if not isinstance(features, dict):
                problems.append("market_features_json_not_object")
        except (TypeError, json.JSONDecodeError):
            problems.append("invalid_market_features_json")
        if problems:
            invalid_origins.append(
                {"origin_at": row["origin_at"], "problems": problems}
            )
    if invalid_origins:
        _issue(
            report,
            Issue(
                "invalid_origin_fields",
                "error",
                "Forecast-origin rows contain invalid required fields.",
                count=len(invalid_origins),
                examples=invalid_origins[:5],
            ),
        )

    invalid_predictions: list[dict[str, Any]] = []
    predictions = connection.execute(
        """
        SELECT p.*, o.source_price_usd
        FROM forecast_predictions AS p
        LEFT JOIN forecast_origins AS o USING(origin_at)
        ORDER BY p.origin_at, p.horizon_hours, p.model_name
        """
    ).fetchall()
    for row in predictions:
        problems: list[str] = []
        if not isinstance(row["model_name"], str) or not row["model_name"].strip():
            problems.append("empty_model_name")
        try:
            horizon = int(row["horizon_hours"])
            if horizon <= 0:
                problems.append("invalid_horizon_hours")
        except (TypeError, ValueError):
            problems.append("invalid_horizon_hours")
        try:
            _parse_timestamp(row["target_at"])
        except (TypeError, ValueError):
            problems.append("invalid_target_at")
        if not _is_finite_number(row["predicted_price_usd"], positive=True):
            problems.append("invalid_predicted_price_usd")
        if not _is_finite_number(row["predicted_change_pct"]):
            problems.append("invalid_predicted_change_pct")
        if row["actual_target_price_usd"] is not None and not _is_finite_number(
            row["actual_target_price_usd"], positive=True
        ):
            problems.append("invalid_actual_target_price_usd")
        if problems:
            invalid_predictions.append(
                {
                    "origin_at": row["origin_at"],
                    "model_name": row["model_name"],
                    "horizon_hours": row["horizon_hours"],
                    "problems": problems,
                }
            )
    if invalid_predictions:
        _issue(
            report,
            Issue(
                "invalid_prediction_fields",
                "error",
                "Prediction rows contain invalid required fields.",
                count=len(invalid_predictions),
                examples=invalid_predictions[:5],
            ),
        )

    target_mismatches: list[dict[str, Any]] = []
    change_mismatches: list[dict[str, Any]] = []
    partial_outcomes: list[dict[str, Any]] = []
    missing_matured: list[dict[str, Any]] = []
    grace = timedelta(minutes=max(0, int(maturity_grace_minutes)))

    for row in predictions:
        if row["source_price_usd"] is None:
            continue
        try:
            origin_dt = _parse_timestamp(row["origin_at"])
            target_dt = _parse_timestamp(row["target_at"])
            horizon = int(row["horizon_hours"])
            source_price = float(row["source_price_usd"])
            predicted = float(row["predicted_price_usd"])
        except (TypeError, ValueError):
            continue
        if horizon <= 0 or source_price <= 0 or predicted <= 0:
            continue

        expected_target = origin_dt + timedelta(hours=horizon)
        if target_dt != expected_target:
            item = {
                "origin_at": row["origin_at"],
                "model_name": row["model_name"],
                "horizon_hours": horizon,
                "stored_target_at": row["target_at"],
                "expected_target_at": _iso(expected_target),
            }
            target_mismatches.append(item)
            _propose(report, {"action": "recompute_target_at", **item})

        expected_change = (predicted / source_price - 1.0) * 100.0
        if not _close_enough(row["predicted_change_pct"], expected_change, 1e-7):
            item = {
                "origin_at": row["origin_at"],
                "model_name": row["model_name"],
                "horizon_hours": horizon,
                "stored_predicted_change_pct": row["predicted_change_pct"],
                "expected_predicted_change_pct": expected_change,
            }
            change_mismatches.append(item)
            _propose(report, {"action": "recompute_predicted_change_pct", **item})

        actual = row["actual_target_price_usd"]
        if actual is None:
            if now >= expected_target + grace:
                target_timestamp = int(expected_target.timestamp())
                supplied = actual_by_timestamp.get(target_timestamp)
                item = {
                    "origin_at": row["origin_at"],
                    "model_name": row["model_name"],
                    "horizon_hours": horizon,
                    "target_at": _iso(expected_target),
                    "actual_available": supplied is not None,
                }
                missing_matured.append(item)
                if supplied is not None:
                    _propose(
                        report,
                        {
                            "action": "fill_matured_outcome",
                            **item,
                            "actual_target_price_usd": float(supplied),
                        },
                    )
            continue

        try:
            actual_price = float(actual)
        except (TypeError, ValueError):
            continue
        if actual_price <= 0:
            continue

        error = predicted - actual_price
        q10 = row["q10_usd"]
        q90 = row["q90_usd"]
        expected_interval = (
            int(float(q10) <= actual_price <= float(q90))
            if q10 is not None and q90 is not None
            else None
        )
        expected = {
            "absolute_error_usd": abs(error),
            "absolute_error_pct": abs(error) / actual_price * 100.0,
            "signed_error_pct": error / actual_price * 100.0,
            "actual_change_pct": (actual_price / source_price - 1.0) * 100.0,
            "direction_correct": int(
                _direction(predicted - source_price)
                == _direction(actual_price - source_price)
            ),
            "within_q10_q90": expected_interval,
        }
        inconsistent: list[str] = []
        for column, value in expected.items():
            stored = row[column]
            if value is None:
                if stored is not None:
                    inconsistent.append(column)
            elif column in ("direction_correct", "within_q10_q90"):
                if stored is None or int(stored) != int(value):
                    inconsistent.append(column)
            elif not _close_enough(stored, value, 1e-7):
                inconsistent.append(column)
        if not row["matured_at"]:
            inconsistent.append("matured_at")

        if inconsistent:
            item = {
                "origin_at": row["origin_at"],
                "model_name": row["model_name"],
                "horizon_hours": horizon,
                "fields": sorted(set(inconsistent)),
                "actual_target_price_usd": actual_price,
            }
            partial_outcomes.append(item)
            _propose(report, {"action": "recompute_outcome_metrics", **item})

    if target_mismatches:
        _issue(
            report,
            Issue(
                "target_timestamp_mismatch",
                "error",
                "Stored target timestamps do not equal origin + horizon.",
                count=len(target_mismatches),
                repairable=True,
                proposed_action=(
                    "recompute target_at from immutable origin_at + horizon_hours"
                ),
                examples=target_mismatches[:5],
            ),
        )
    if change_mismatches:
        _issue(
            report,
            Issue(
                "predicted_change_mismatch",
                "error",
                "Stored predicted-change percentages disagree with source/predicted prices.",
                count=len(change_mismatches),
                repairable=True,
                proposed_action="recompute predicted_change_pct",
                examples=change_mismatches[:5],
            ),
        )
    if partial_outcomes:
        _issue(
            report,
            Issue(
                "incomplete_or_inconsistent_outcome_metrics",
                "error",
                "Matured outcomes have missing or inconsistent derived metrics.",
                count=len(partial_outcomes),
                repairable=True,
                proposed_action=(
                    "recompute outcome metrics from immutable prediction, source and actual"
                ),
                examples=partial_outcomes[:5],
            ),
        )
    if missing_matured:
        available = sum(1 for item in missing_matured if item["actual_available"])
        _issue(
            report,
            Issue(
                "missing_matured_outcomes",
                "warning",
                "Targets older than the maturity grace period still have no actual outcome.",
                count=len(missing_matured),
                repairable=available > 0,
                proposed_action=(
                    f"fill {available} outcome(s) from the explicitly supplied exact-target price map"
                    if available
                    else "supply --actuals with exact target timestamps to repair safely"
                ),
                examples=missing_matured[:5],
            ),
        )


def _apply_repairs(path: Path, report: dict[str, Any], *, now: datetime) -> None:
    proposed = list(report["repairs"]["proposed"])
    repairable_actions = {
        "recompute_target_at",
        "recompute_predicted_change_pct",
        "recompute_outcome_metrics",
        "fill_matured_outcome",
    }
    writable = [
        item for item in proposed if item.get("action") in repairable_actions
    ]
    if not writable:
        return

    backup = _backup_path(path, now)
    shutil.copy2(path, backup)
    report["repairs"]["backup"] = str(backup)

    applied: list[dict[str, Any]] = []
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        for action in writable:
            name = action["action"]
            key = (
                action["origin_at"],
                action["model_name"],
                int(action["horizon_hours"]),
            )
            if name == "recompute_target_at":
                cursor = connection.execute(
                    """
                    UPDATE forecast_predictions
                    SET target_at = ?
                    WHERE origin_at = ? AND model_name = ? AND horizon_hours = ?
                    """,
                    (action["expected_target_at"], *key),
                )
            elif name == "recompute_predicted_change_pct":
                cursor = connection.execute(
                    """
                    UPDATE forecast_predictions
                    SET predicted_change_pct = ?
                    WHERE origin_at = ? AND model_name = ? AND horizon_hours = ?
                    """,
                    (float(action["expected_predicted_change_pct"]), *key),
                )
            elif name == "fill_matured_outcome":
                actual = float(action["actual_target_price_usd"])
                row = connection.execute(
                    """
                    SELECT p.*, o.source_price_usd
                    FROM forecast_predictions AS p
                    JOIN forecast_origins AS o USING(origin_at)
                    WHERE p.origin_at = ? AND p.model_name = ? AND p.horizon_hours = ?
                    """,
                    key,
                ).fetchone()
                if row is None or row["actual_target_price_usd"] is not None:
                    continue
                cursor = _write_outcome(connection, key, row, actual, now)
            elif name == "recompute_outcome_metrics":
                row = connection.execute(
                    """
                    SELECT p.*, o.source_price_usd
                    FROM forecast_predictions AS p
                    JOIN forecast_origins AS o USING(origin_at)
                    WHERE p.origin_at = ? AND p.model_name = ? AND p.horizon_hours = ?
                    """,
                    key,
                ).fetchone()
                if row is None or row["actual_target_price_usd"] is None:
                    continue
                cursor = _write_outcome(
                    connection,
                    key,
                    row,
                    float(row["actual_target_price_usd"]),
                    now,
                    preserve_actual=True,
                )
            else:
                continue

            if cursor.rowcount:
                applied.append(action)
        connection.commit()
    except Exception:
        connection.rollback()
        connection.close()
        shutil.copy2(backup, path)
        raise
    finally:
        connection.close()

    report["repairs"]["applied"] = applied


def _write_outcome(
    connection: sqlite3.Connection,
    key: tuple[Any, Any, int],
    row: sqlite3.Row,
    actual: float,
    now: datetime,
    *,
    preserve_actual: bool = False,
) -> sqlite3.Cursor:
    predicted = float(row["predicted_price_usd"])
    source_price = float(row["source_price_usd"])
    error = predicted - actual
    q10 = row["q10_usd"]
    q90 = row["q90_usd"]
    within = (
        int(float(q10) <= actual <= float(q90))
        if q10 is not None and q90 is not None
        else None
    )
    matured_at = row["matured_at"] or _iso(now)
    sql = """
        UPDATE forecast_predictions
        SET actual_target_price_usd = ?,
            absolute_error_usd = ?,
            absolute_error_pct = ?,
            signed_error_pct = ?,
            actual_change_pct = ?,
            direction_correct = ?,
            within_q10_q90 = ?,
            matured_at = ?
        WHERE origin_at = ? AND model_name = ? AND horizon_hours = ?
    """
    params: tuple[Any, ...] = (
        actual,
        abs(error),
        abs(error) / actual * 100.0,
        error / actual * 100.0,
        (actual / source_price - 1.0) * 100.0,
        int(
            _direction(predicted - source_price)
            == _direction(actual - source_price)
        ),
        within,
        matured_at,
        *key,
    )
    if preserve_actual:
        return connection.execute(sql, params)
    return connection.execute(sql + " AND actual_target_price_usd IS NULL", params)


def audit_database(
    path: Path | str,
    *,
    repair: bool = False,
    actual_by_timestamp: dict[int, float] | None = None,
    now: datetime | None = None,
    maturity_grace_minutes: int = DEFAULT_GRACE_MINUTES,
) -> dict[str, Any]:
    db_path = Path(path)
    checked_at = (now or _utc_now()).astimezone(timezone.utc)
    actuals = actual_by_timestamp or {}
    report = _base_report(db_path, "repair" if repair else "dry-run", checked_at)

    if not db_path.exists() or db_path.stat().st_size == 0:
        _issue(
            report,
            Issue(
                "database_missing",
                "error",
                "Forecast-history database is missing or empty.",
            ),
        )
        return _finalize_report(report)

    uri = f"file:{db_path.resolve()}?mode=ro"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True)
        _audit_connection(
            connection,
            report,
            now=checked_at,
            maturity_grace_minutes=maturity_grace_minutes,
            actual_by_timestamp=actuals,
        )
    except sqlite3.DatabaseError as exc:
        _issue(
            report,
            Issue(
                "sqlite_read_failure",
                "error",
                f"SQLite could not read the database: {exc}",
            ),
        )
        return _finalize_report(report)
    finally:
        if connection is not None:
            connection.close()

    _finalize_report(report)
    if not repair:
        return report

    structural_codes = {
        "sqlite_integrity",
        "sqlite_read_failure",
        "missing_tables",
        "missing_columns",
        "schema_version",
        "duplicate_origins",
        "duplicate_predictions",
        "foreign_key_violation",
        "orphan_prediction_rows",
        "invalid_origin_fields",
        "invalid_prediction_fields",
    }
    blockers = sorted(
        issue["code"]
        for issue in report["issues"]
        if issue["code"] in structural_codes
    )
    if blockers:
        report["repairs"]["blocked_reason"] = (
            "Automatic repair was blocked by structural/required-field errors: "
            + ", ".join(blockers)
        )
        return _finalize_report(report)

    _apply_repairs(db_path, report, now=checked_at)

    repaired = _base_report(db_path, "repair", checked_at)
    repaired["repairs"] = report["repairs"]
    connection = None
    try:
        connection = sqlite3.connect(uri, uri=True)
        _audit_connection(
            connection,
            repaired,
            now=checked_at,
            maturity_grace_minutes=maturity_grace_minutes,
            actual_by_timestamp=actuals,
        )
    except sqlite3.DatabaseError as exc:
        _issue(
            repaired,
            Issue(
                "sqlite_read_failure",
                "error",
                f"SQLite could not read the database after repair: {exc}",
            ),
        )
    finally:
        if connection is not None:
            connection.close()
    return _finalize_report(repaired)


def write_report(report: dict[str, Any], path: Path | str | None) -> None:
    if path is None:
        return
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _exit_code(report: dict[str, Any], fail_on: str) -> int:
    if fail_on == "never":
        return 0
    if report["summary"]["errors"] > 0:
        return 2
    if fail_on == "warning" and report["summary"]["warnings"] > 0:
        return 2
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit and safely repair the durable BTC forecast-history database"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--actuals", type=Path)
    parser.add_argument("--repair", action="store_true")
    parser.add_argument(
        "--maturity-grace-minutes",
        type=int,
        default=DEFAULT_GRACE_MINUTES,
    )
    parser.add_argument(
        "--fail-on",
        choices=("error", "warning", "never"),
        default="error",
        help="Exit non-zero when the selected severity is present (default: error).",
    )
    parser.add_argument(
        "--now",
        help="Override current UTC time for deterministic auditing/testing.",
    )
    args = parser.parse_args()

    now = _parse_timestamp(args.now) if args.now else None
    actuals = load_actuals(args.actuals)
    report = audit_database(
        args.db,
        repair=args.repair,
        actual_by_timestamp=actuals,
        now=now,
        maturity_grace_minutes=args.maturity_grace_minutes,
    )
    write_report(report, args.report)
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    raise SystemExit(_exit_code(report, args.fail_on))


if __name__ == "__main__":
    main()
