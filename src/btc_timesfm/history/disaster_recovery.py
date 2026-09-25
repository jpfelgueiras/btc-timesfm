#!/usr/bin/env python3
"""Restore and verify a forecast-history backup without touching production history."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from btc_timesfm.history.history_audit import audit_database
from btc_timesfm.history.history_backup import restore_archive
from btc_timesfm.history.history_migrations import CURRENT_SCHEMA_VERSION
from btc_timesfm.ops.observability import PipelineObserver

REPORT_VERSION = 1
DEFAULT_RECENCY_DAYS = 30


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _row_counts(path: Path) -> dict[str, int]:
    uri = f"file:{path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        return {
            "forecast_origins": int(
                connection.execute("SELECT COUNT(*) FROM forecast_origins").fetchone()[0]
            ),
            "forecast_predictions": int(
                connection.execute("SELECT COUNT(*) FROM forecast_predictions").fetchone()[0]
            ),
            "matured_predictions": int(
                connection.execute(
                    "SELECT COUNT(*) FROM forecast_predictions WHERE actual_target_price_usd IS NOT NULL"
                ).fetchone()[0]
            ),
        }


def run_drill(
    archive_path: Path | str,
    *,
    now: datetime | None = None,
    recency_days: int = DEFAULT_RECENCY_DAYS,
    scratch_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Restore an archive into scratch storage and return a non-mutating drill report."""
    if recency_days < 1:
        raise ValueError("recency_days must be >= 1")
    checked_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    archive = Path(archive_path)
    started = time.perf_counter()
    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "checked_at": _iso(checked_at),
        "archive": str(archive),
        "schema_version_expected": CURRENT_SCHEMA_VERSION,
        "recency_days": recency_days,
        "status": "failed",
        "restore_duration_ms": None,
        "row_counts": {},
        "recent_content": {},
        "verification": {},
        "audit": {},
        "failure": None,
    }
    try:
        with tempfile.TemporaryDirectory(
            prefix="forecast-history-drill-", dir=scratch_dir
        ) as directory:
            restored = Path(directory) / "forecast_history.sqlite"
            restored_report = restore_archive(archive, restored)
            report["restore_duration_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
            report["verification"] = restored_report["restored_database_verification"]
            report["row_counts"] = _row_counts(restored)
            audit = audit_database(restored, now=checked_at)
            report["audit_before_repair"] = audit
            if not audit["healthy"] and audit["summary"]["repairable_issues"]:
                # Exercise the supported, backup-producing repair on the
                # restored scratch copy only. This never changes the archive
                # or production history and verifies that repairable legacy
                # derived-field drift can be recovered.
                audit = audit_database(restored, repair=True, now=checked_at)
                report["repair"] = audit["repairs"]
            report["audit"] = audit
            latest_origin: str | None
            with sqlite3.connect(f"file:{restored.resolve()}?mode=ro", uri=True) as connection:
                value = connection.execute(
                    "SELECT MAX(origin_at) FROM forecast_origins"
                ).fetchone()[0]
                latest_origin = str(value) if value is not None else None
            cutoff = checked_at - timedelta(days=recency_days)
            recent = latest_origin is not None and _parse_time(latest_origin) >= cutoff
            report["recent_content"] = {
                "latest_origin_at": latest_origin,
                "cutoff_at": _iso(cutoff),
                "recent": recent,
            }
            if not audit["healthy"]:
                raise RuntimeError("restored database audit reported errors")
            if report["row_counts"]["forecast_origins"] < 1:
                raise RuntimeError("restored database has no forecast origins")
            if report["row_counts"]["forecast_predictions"] < 1:
                raise RuntimeError("restored database has no forecast predictions")
            if not recent:
                raise RuntimeError("restored database has no recent forecast origin")
            report["status"] = "passed"
    except Exception as exc:
        report["restore_duration_ms"] = report["restore_duration_ms"] or round(
            (time.perf_counter() - started) * 1000.0, 3
        )
        report["failure"] = {"type": type(exc).__name__, "message": str(exc)}
    return report


def write_report(report: dict[str, Any], path: Path | str) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a non-mutating forecast-history recovery drill"
    )
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--recency-days", type=int, default=DEFAULT_RECENCY_DAYS)
    parser.add_argument("--scratch-dir", type=Path)
    parser.add_argument("--now")
    args = parser.parse_args()
    now = _parse_time(args.now) if args.now else None
    observer = PipelineObserver(run_type="disaster_recovery_drill")
    with observer.stage("restore_and_verify", archive=str(args.archive)):
        report = run_drill(
            args.archive,
            now=now,
            recency_days=args.recency_days,
            scratch_dir=args.scratch_dir,
        )
    write_report(report, args.report)
    observer.metadata(disaster_recovery=report)
    observer.finalize("success" if report["status"] == "passed" else "failed")
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    raise SystemExit(0 if report["status"] == "passed" else 2)


if __name__ == "__main__":
    main()
