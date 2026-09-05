#!/usr/bin/env python3
"""Audit and safely repair the durable forecast-history SQLite database."""

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


@dataclass(frozen=True)
class Issue:
    code: str
    severity: str
    message: str
    count: int = 1
    repairable: bool = False
    proposed_action: str | None = None
    examples: list[dict[str, Any]] | None = None

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "count": self.count,
            "repairable": self.repairable,
        }
        if self.proposed_action:
            value["proposed_action"] = self.proposed_action
        if self.examples:
            value["examples"] = self.examples
        return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp must be a non-empty string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _finite(value: Any, *, positive: bool = False) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(number):
        return False
    return number > 0 if positive else True


def _close(left: Any, right: Any, tolerance: float = 1e-8) -> bool:
    if left is None or right is None:
        return left is right
    try:
        a = float(left)
        b = float(right)
    except (TypeError, ValueError):
        return False
    return abs(a - b) <= tolerance * max(1.0, abs(a), abs(b))


def _direction(value: float, epsilon: float = 1e-9) -> int:
    if value > epsilon:
        return 1
    if value < -epsilon:
        return -1
    return 0


def _examples(rows: Iterable[sqlite3.Row], limit: int = 5) -> list[dict[str, Any]]:
    return [dict(row) for index, row in enumerate(rows) if index < limit]


def _report(path: Path, mode: str, now: datetime) -> dict[str, Any]:
    return {
        "report_version": AUDIT_REPORT_VERSION,
        "database": str(path),
        "mode": mode,
        "checked_at": _iso(now),
        "supported_schema_version": CURRENT_SCHEMA_VERSION,
        "healthy": False,
        "issues": [],
        "repairs": {"backup": None, "proposed": [], "applied": []},
        "summary": {
            "errors": 0,
            "warnings": 0,
            "info": 0,
            "repairable_issues": 0,
            "proposed_actions": 0,
            "applied_actions": 0,
        },
    }


def _add_issue(report: dict[str, Any], issue: Issue) -> None:
    report["issues"].append(issue.as_dict())


def _propose(report: dict[str, Any], action: dict[str, Any]) -> None:
    report["repairs"]["proposed"].append(action)


def _finalize(report: dict[str, Any]) -> dict[str, Any]:
    issues = report["issues"]
    summary = report["summary"]
    summary["errors"] = sum(item["severity"] == "error" for item in issues)
    summary["warnings"] = sum(item["severity"] == "warning" for item in issues)
    summary["info"] = sum(item["severity"] == "info" for item in issues)
    summary["repairable_issues"] = sum(bool(item["repairable"]) for item in issues)
    summary["proposed_actions"] = len(report["repairs"]["proposed"])
    summary["applied_actions"] = len(report["repairs"]["applied"])
    report["healthy"] = summary["errors"] == 0
    return report


def load_actuals(path: Path | str | None) -> dict[int, float]:
    """Load explicit exact-target prices from a JSON mapping or list."""
    if path is None:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    result: dict[int, float] = {}

    def add(key: Any, value: Any) -> None:
        if not _finite(value, positive=True):
            raise ValueError(f"Invalid positive price for {key!r}: {value!r}")
        if isinstance(key, (int, float)):
            timestamp = int(key)
        elif isinstance(key, str) and key.strip().isdigit():
            timestamp = int(key.strip())
        else:
            timestamp = int(_parse_timestamp(str(key)).timestamp())
        result[timestamp] = float(value)

    if isinstance(payload, dict):
        for key, value in payload.items():
            add(key, value)
        return result
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                raise ValueError("Actuals list entries must be JSON objects")
            if "target_at" not in item or "actual_target_price_usd" not in item:
                raise ValueError(
                    "Actuals entries require target_at and actual_target_price_usd"
                )
            add(item["target_at"], item["actual_target_price_usd"])
        return result
    raise ValueError("Actuals JSON must be an object or list")


def _required_structure(connection: sqlite3.Connection, report: dict[str, Any]) -> bool:
    integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
    report["sqlite_integrity"] = integrity
    if integrity != ["ok"]:
        _add_issue(
            report,
            Issue(
                "sqlite_integrity",
                "error",
                "SQLite integrity_check reported corruption.",
                len(integrity),
                examples=[{"message": value} for value in integrity[:5]],
            ),
        )

    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    report["tables"] = sorted(tables)
    required_tables = {
        "metadata",
        "forecast_origins",
        "forecast_predictions",
        "schema_migrations",
    }
    missing_tables = sorted(required_tables - tables)
    if missing_tables:
        _add_issue(
            report,
            Issue(
                "missing_tables",
                "error",
                "History database is missing required tables.",
                len(missing_tables),
                examples=[{"table": name} for name in missing_tables],
            ),
        )
        return False

    required_columns: dict[str, set[str]] = {
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
    for table_name, expected_columns in required_columns.items():
        existing_columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table_name})")
        }
        missing = sorted(expected_columns - existing_columns)
        if missing:
            missing_columns.append({"table": table_name, "columns": missing})
    if missing_columns:
        _add_issue(
            report,
            Issue(
                "missing_columns",
                "error",
                "History database tables are missing required columns.",
                sum(len(item["columns"]) for item in missing_columns),
                examples=missing_columns[:5],
            ),
        )
        return False

    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    report["schema_version"] = version
    if version != CURRENT_SCHEMA_VERSION:
        _add_issue(
            report,
            Issue(
                "schema_version",
                "error",
                f"Schema version {version} does not match "
                f"{CURRENT_SCHEMA_VERSION}; migrate it first.",
            ),
        )

    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    report["foreign_key_violations"] = len(foreign_keys)
    if foreign_keys:
        _add_issue(
            report,
            Issue(
                "foreign_key_violation",
                "error",
                "Foreign-key violations exist; automatic repair will not delete rows.",
                len(foreign_keys),
                examples=_examples(foreign_keys),
            ),
        )
    return True


def _audit_uniqueness(connection: sqlite3.Connection, report: dict[str, Any]) -> None:
    origins = connection.execute(
        """
        SELECT origin_at, COUNT(*) AS duplicate_count
        FROM forecast_origins
        GROUP BY origin_at
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    if origins:
        _add_issue(
            report,
            Issue(
                "duplicate_origins",
                "error",
                "Duplicate logical forecast origins were found.",
                len(origins),
                examples=_examples(origins),
            ),
        )

    predictions = connection.execute(
        """
        SELECT origin_at, model_name, horizon_hours, COUNT(*) AS duplicate_count
        FROM forecast_predictions
        GROUP BY origin_at, model_name, horizon_hours
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    if predictions:
        _add_issue(
            report,
            Issue(
                "duplicate_predictions",
                "error",
                "Duplicate logical prediction keys were found.",
                len(predictions),
                examples=_examples(predictions),
            ),
        )


def _audit_orphans(connection: sqlite3.Connection, report: dict[str, Any]) -> None:
    predictions = connection.execute(
        """
        SELECT p.origin_at, p.model_name, p.horizon_hours
        FROM forecast_predictions AS p
        LEFT JOIN forecast_origins AS o USING(origin_at)
        WHERE o.origin_at IS NULL
        ORDER BY p.origin_at, p.horizon_hours, p.model_name
        """
    ).fetchall()
    if predictions:
        _add_issue(
            report,
            Issue(
                "orphan_prediction_rows",
                "error",
                "Prediction rows reference a missing origin; they are never auto-deleted.",
                len(predictions),
                examples=_examples(predictions),
            ),
        )

    groups = connection.execute(
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
    if groups:
        _add_issue(
            report,
            Issue(
                "orphan_model_groups",
                "error",
                "Underlying model rows exist without the matching ensemble row.",
                len(groups),
                examples=_examples(groups),
            ),
        )


def _audit_origins(connection: sqlite3.Connection, report: dict[str, Any]) -> None:
    invalid: list[dict[str, Any]] = []
    rows = connection.execute(
        """
        SELECT origin_at, generated_at, source_price_usd, market_features_json,
               first_seen_at, last_seen_at
        FROM forecast_origins
        ORDER BY origin_at
        """
    ).fetchall()
    for row in rows:
        row_problems: list[str] = []
        for column in ("origin_at", "generated_at", "first_seen_at", "last_seen_at"):
            try:
                _parse_timestamp(row[column])
            except (TypeError, ValueError):
                row_problems.append(f"invalid_{column}")
        if not _finite(row["source_price_usd"], positive=True):
            row_problems.append("invalid_source_price_usd")
        try:
            features = json.loads(row["market_features_json"])
            if not isinstance(features, dict):
                row_problems.append("market_features_json_not_object")
        except (TypeError, json.JSONDecodeError):
            row_problems.append("invalid_market_features_json")
        if row_problems:
            invalid.append({"origin_at": row["origin_at"], "problems": row_problems})
    if invalid:
        _add_issue(
            report,
            Issue(
                "invalid_origin_fields",
                "error",
                "Forecast-origin rows contain invalid required fields.",
                len(invalid),
                examples=invalid[:5],
            ),
        )


def _expected_outcome(
    row: sqlite3.Row,
    source_price: float,
    predicted: float,
    actual: float,
) -> dict[str, float | int | None]:
    error = predicted - actual
    q10 = row["q10_usd"]
    q90 = row["q90_usd"]
    interval = (
        int(float(q10) <= actual <= float(q90))
        if q10 is not None and q90 is not None
        else None
    )
    return {
        "absolute_error_usd": abs(error),
        "absolute_error_pct": abs(error) / actual * 100.0,
        "signed_error_pct": error / actual * 100.0,
        "actual_change_pct": (actual / source_price - 1.0) * 100.0,
        "direction_correct": int(
            _direction(predicted - source_price)
            == _direction(actual - source_price)
        ),
        "within_q10_q90": interval,
    }


def _audit_predictions(
    connection: sqlite3.Connection,
    report: dict[str, Any],
    *,
    now: datetime,
    maturity_grace_minutes: int,
    actual_by_timestamp: dict[int, float],
) -> None:
    rows = connection.execute(
        """
        SELECT p.*, o.source_price_usd
        FROM forecast_predictions AS p
        LEFT JOIN forecast_origins AS o USING(origin_at)
        ORDER BY p.origin_at, p.horizon_hours, p.model_name
        """
    ).fetchall()

    invalid: list[dict[str, Any]] = []
    target_mismatches: list[dict[str, Any]] = []
    change_mismatches: list[dict[str, Any]] = []
    outcome_mismatches: list[dict[str, Any]] = []
    missing_matured: list[dict[str, Any]] = []
    grace = timedelta(minutes=max(0, maturity_grace_minutes))

    for row in rows:
        row_problems: list[str] = []
        if not isinstance(row["model_name"], str) or not row["model_name"].strip():
            row_problems.append("empty_model_name")
        if not _finite(row["predicted_price_usd"], positive=True):
            row_problems.append("invalid_predicted_price_usd")
        if not _finite(row["predicted_change_pct"]):
            row_problems.append("invalid_predicted_change_pct")
        if row["source_price_usd"] is None:
            continue

        try:
            horizon = int(row["horizon_hours"])
            origin = _parse_timestamp(row["origin_at"])
            target = _parse_timestamp(row["target_at"])
        except (TypeError, ValueError):
            row_problems.append("invalid_horizon_or_timestamp")
            invalid.append(
                {
                    "origin_at": row["origin_at"],
                    "model_name": row["model_name"],
                    "horizon_hours": row["horizon_hours"],
                    "problems": row_problems,
                }
            )
            continue
        if horizon <= 0:
            row_problems.append("invalid_horizon_hours")
        if row["actual_target_price_usd"] is not None and not _finite(
            row["actual_target_price_usd"], positive=True
        ):
            row_problems.append("invalid_actual_target_price_usd")
        if row_problems:
            invalid.append(
                {
                    "origin_at": row["origin_at"],
                    "model_name": row["model_name"],
                    "horizon_hours": horizon,
                    "problems": row_problems,
                }
            )
            continue

        source = float(row["source_price_usd"])
        predicted = float(row["predicted_price_usd"])
        expected_target = origin + timedelta(hours=horizon)
        key = {
            "origin_at": row["origin_at"],
            "model_name": row["model_name"],
            "horizon_hours": horizon,
        }

        if target != expected_target:
            item = {
                **key,
                "stored_target_at": row["target_at"],
                "expected_target_at": _iso(expected_target),
            }
            target_mismatches.append(item)
            _propose(report, {"action": "recompute_target_at", **item})

        expected_change = (predicted / source - 1.0) * 100.0
        if not _close(row["predicted_change_pct"], expected_change, 1e-7):
            item = {
                **key,
                "stored_predicted_change_pct": row["predicted_change_pct"],
                "expected_predicted_change_pct": expected_change,
            }
            change_mismatches.append(item)
            _propose(report, {"action": "recompute_predicted_change_pct", **item})

        actual_value = row["actual_target_price_usd"]
        if actual_value is None:
            if now >= expected_target + grace:
                supplied = actual_by_timestamp.get(int(expected_target.timestamp()))
                item = {
                    **key,
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

        actual = float(actual_value)
        expected_metrics = _expected_outcome(row, source, predicted, actual)
        inconsistent: list[str] = []
        for column, expected_value in expected_metrics.items():
            stored = row[column]
            if expected_value is None:
                if stored is not None:
                    inconsistent.append(column)
            elif column in {"direction_correct", "within_q10_q90"}:
                if stored is None or int(stored) != int(expected_value):
                    inconsistent.append(column)
            elif not _close(stored, expected_value, 1e-7):
                inconsistent.append(column)
        if not row["matured_at"]:
            inconsistent.append("matured_at")
        if inconsistent:
            item = {
                **key,
                "fields": sorted(set(inconsistent)),
                "actual_target_price_usd": actual,
            }
            outcome_mismatches.append(item)
            _propose(report, {"action": "recompute_outcome_metrics", **item})

    if invalid:
        _add_issue(
            report,
            Issue(
                "invalid_prediction_fields",
                "error",
                "Prediction rows contain invalid required fields.",
                len(invalid),
                examples=invalid[:5],
            ),
        )
    if target_mismatches:
        _add_issue(
            report,
            Issue(
                "target_timestamp_mismatch",
                "error",
                "Stored target timestamps do not equal origin + horizon.",
                len(target_mismatches),
                True,
                "recompute target_at from immutable origin_at + horizon_hours",
                target_mismatches[:5],
            ),
        )
    if change_mismatches:
        _add_issue(
            report,
            Issue(
                "predicted_change_mismatch",
                "error",
                "Stored predicted-change values disagree with stored prices.",
                len(change_mismatches),
                True,
                "recompute predicted_change_pct",
                change_mismatches[:5],
            ),
        )
    if outcome_mismatches:
        _add_issue(
            report,
            Issue(
                "incomplete_or_inconsistent_outcome_metrics",
                "error",
                "Matured outcomes have missing or inconsistent derived metrics.",
                len(outcome_mismatches),
                True,
                "recompute derived metrics from immutable prices",
                outcome_mismatches[:5],
            ),
        )
    if missing_matured:
        available = sum(bool(item["actual_available"]) for item in missing_matured)
        action = (
            f"fill {available} outcome(s) from supplied exact-target prices"
            if available
            else "supply --actuals with exact target timestamps to repair safely"
        )
        _add_issue(
            report,
            Issue(
                "missing_matured_outcomes",
                "warning",
                "Mature targets still have no stored actual outcome.",
                len(missing_matured),
                available > 0,
                action,
                missing_matured[:5],
            ),
        )


def _audit(
    connection: sqlite3.Connection,
    report: dict[str, Any],
    *,
    now: datetime,
    maturity_grace_minutes: int,
    actual_by_timestamp: dict[int, float],
) -> None:
    connection.row_factory = sqlite3.Row
    if not _required_structure(connection, report):
        return
    _audit_uniqueness(connection, report)
    _audit_orphans(connection, report)
    _audit_origins(connection, report)
    _audit_predictions(
        connection,
        report,
        now=now,
        maturity_grace_minutes=maturity_grace_minutes,
        actual_by_timestamp=actual_by_timestamp,
    )


def _backup_path(path: Path, now: datetime) -> Path:
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return path.with_name(f"{path.name}.pre-repair-{stamp}.bak")


def _write_outcome(
    connection: sqlite3.Connection,
    key: tuple[str, str, int],
    actual: float,
    now: datetime,
    *,
    only_if_missing: bool,
) -> sqlite3.Cursor:
    row = connection.execute(
        """
        SELECT p.*, o.source_price_usd
        FROM forecast_predictions AS p
        JOIN forecast_origins AS o USING(origin_at)
        WHERE p.origin_at = ? AND p.model_name = ? AND p.horizon_hours = ?
        """,
        key,
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Prediction disappeared during repair: {key!r}")
    source = float(row["source_price_usd"])
    predicted = float(row["predicted_price_usd"])
    expected = _expected_outcome(row, source, predicted, actual)
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
    if only_if_missing:
        sql += " AND actual_target_price_usd IS NULL"
    return connection.execute(
        sql,
        (
            actual,
            expected["absolute_error_usd"],
            expected["absolute_error_pct"],
            expected["signed_error_pct"],
            expected["actual_change_pct"],
            expected["direction_correct"],
            expected["within_q10_q90"],
            matured_at,
            *key,
        ),
    )


def _apply_repairs(path: Path, report: dict[str, Any], now: datetime) -> None:
    actions = list(report["repairs"]["proposed"])
    writable = {
        "recompute_target_at",
        "recompute_predicted_change_pct",
        "recompute_outcome_metrics",
        "fill_matured_outcome",
    }
    actions = [action for action in actions if action["action"] in writable]
    if not actions:
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
        for action in actions:
            key = (
                str(action["origin_at"]),
                str(action["model_name"]),
                int(action["horizon_hours"]),
            )
            action_name = action["action"]
            if action_name == "recompute_target_at":
                cursor = connection.execute(
                    """
                    UPDATE forecast_predictions SET target_at = ?
                    WHERE origin_at = ? AND model_name = ? AND horizon_hours = ?
                    """,
                    (action["expected_target_at"], *key),
                )
            elif action_name == "recompute_predicted_change_pct":
                cursor = connection.execute(
                    """
                    UPDATE forecast_predictions SET predicted_change_pct = ?
                    WHERE origin_at = ? AND model_name = ? AND horizon_hours = ?
                    """,
                    (float(action["expected_predicted_change_pct"]), *key),
                )
            elif action_name == "fill_matured_outcome":
                cursor = _write_outcome(
                    connection,
                    key,
                    float(action["actual_target_price_usd"]),
                    now,
                    only_if_missing=True,
                )
            else:
                row = connection.execute(
                    """
                    SELECT actual_target_price_usd
                    FROM forecast_predictions
                    WHERE origin_at = ? AND model_name = ? AND horizon_hours = ?
                    """,
                    key,
                ).fetchone()
                if row is None or row["actual_target_price_usd"] is None:
                    continue
                cursor = _write_outcome(
                    connection,
                    key,
                    float(row["actual_target_price_usd"]),
                    now,
                    only_if_missing=False,
                )
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


def audit_database(
    path: Path | str,
    *,
    repair: bool = False,
    actual_by_timestamp: dict[int, float] | None = None,
    now: datetime | None = None,
    maturity_grace_minutes: int = DEFAULT_GRACE_MINUTES,
) -> dict[str, Any]:
    """Audit a forecast-history DB and optionally apply conservative repairs."""
    db_path = Path(path)
    checked_at = (now or _utc_now()).astimezone(timezone.utc)
    actuals = actual_by_timestamp or {}
    report = _report(db_path, "repair" if repair else "dry-run", checked_at)

    if not db_path.exists() or db_path.stat().st_size == 0:
        _add_issue(
            report,
            Issue(
                "database_missing",
                "error",
                "Forecast-history database is missing or empty.",
            ),
        )
        return _finalize(report)

    uri = f"file:{db_path.resolve()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            _audit(
                connection,
                report,
                now=checked_at,
                maturity_grace_minutes=maturity_grace_minutes,
                actual_by_timestamp=actuals,
            )
    except sqlite3.DatabaseError as exc:
        _add_issue(
            report,
            Issue(
                "sqlite_read_failure",
                "error",
                f"SQLite could not read the database: {exc}",
            ),
        )
        return _finalize(report)

    _finalize(report)
    if not repair:
        return report

    unsafe_codes = {
        "sqlite_integrity",
        "sqlite_read_failure",
        "missing_tables",
        "missing_columns",
        "schema_version",
        "foreign_key_violation",
        "duplicate_origins",
        "duplicate_predictions",
        "orphan_prediction_rows",
        "orphan_model_groups",
        "invalid_origin_fields",
        "invalid_prediction_fields",
    }
    blockers = sorted(
        issue["code"] for issue in report["issues"] if issue["code"] in unsafe_codes
    )
    if blockers:
        report["repairs"]["blocked_reason"] = (
            "Automatic repair blocked by structural or ambiguous errors: "
            + ", ".join(blockers)
        )
        return _finalize(report)

    _apply_repairs(db_path, report, checked_at)
    repaired = _report(db_path, "repair", checked_at)
    repaired["repairs"] = report["repairs"]
    with sqlite3.connect(uri, uri=True) as connection:
        _audit(
            connection,
            repaired,
            now=checked_at,
            maturity_grace_minutes=maturity_grace_minutes,
            actual_by_timestamp=actuals,
        )
    return _finalize(repaired)


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
    if report["summary"]["errors"]:
        return 2
    if fail_on == "warning" and report["summary"]["warnings"]:
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
    )
    parser.add_argument("--now")
    args = parser.parse_args()

    checked_at = _parse_timestamp(args.now) if args.now else None
    actuals = load_actuals(args.actuals)
    report = audit_database(
        args.db,
        repair=args.repair,
        actual_by_timestamp=actuals,
        now=checked_at,
        maturity_grace_minutes=args.maturity_grace_minutes,
    )
    write_report(report, args.report)
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    raise SystemExit(_exit_code(report, args.fail_on))


if __name__ == "__main__":
    main()
