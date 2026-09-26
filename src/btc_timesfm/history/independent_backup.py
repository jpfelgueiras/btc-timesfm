#!/usr/bin/env python3
"""Copy, validate, and monitor history archives in an independent S3 bucket."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from btc_timesfm.history.history_backup import verify_archive

MANIFEST_VERSION = 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _aws(*args: str) -> None:
    try:
        subprocess.run(["aws", "s3", *args], check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        # AWS diagnostics can include account, endpoint, or credential details.
        raise RuntimeError("independent S3 backup operation failed") from exc


def _manifest_uri(uri: str) -> str:
    return f"{uri}.manifest.json"


def _receipt_uri(uri: str) -> str:
    return f"{uri}.restore.json"


def _history_summary(archive: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="independent-history-summary-") as directory:
        db = Path(directory) / "history.sqlite"
        with gzip.open(archive, "rb") as source, db.open("wb") as target:
            while chunk := source.read(1024 * 1024):
                target.write(chunk)
        with closing(sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)) as connection:
            counts = {
                "forecast_origins": int(
                    connection.execute("SELECT COUNT(*) FROM forecast_origins").fetchone()[0]
                ),
                "forecast_predictions": int(
                    connection.execute("SELECT COUNT(*) FROM forecast_predictions").fetchone()[0]
                ),
                "matured_predictions": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM forecast_predictions "
                        "WHERE actual_target_price_usd IS NOT NULL"
                    ).fetchone()[0]
                ),
            }
            latest_origin = connection.execute(
                "SELECT MAX(origin_at) FROM forecast_origins"
            ).fetchone()[0]
    return {"row_counts": counts, "latest_origin_at": latest_origin}


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("independent backup manifest is missing or invalid") from exc
    required = {
        "manifest_version",
        "verified_at",
        "archive_bytes",
        "archive_uri",
        "sha256",
        "schema_version",
        "row_counts",
        "latest_origin_at",
    }
    if not isinstance(manifest, dict) or not required.issubset(manifest):
        raise RuntimeError("independent backup manifest is incomplete")
    if manifest["manifest_version"] != MANIFEST_VERSION:
        raise RuntimeError("independent backup manifest version is unsupported")
    try:
        verified_at = datetime.fromisoformat(str(manifest["verified_at"]).replace("Z", "+00:00"))
        if verified_at.tzinfo is None:
            raise ValueError
        if int(manifest["archive_bytes"]) < 1 or len(str(manifest["sha256"])) != 64:
            raise ValueError
        if not isinstance(manifest["row_counts"], dict):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise RuntimeError("independent backup manifest contains invalid values") from exc
    return manifest


def _download_verified(uri: str, destination: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Download archive and manifest to temporary paths, verify, then atomically publish."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="independent-history-restore-", dir=destination.parent
    ) as directory:
        root = Path(directory)
        archive_tmp = root / "archive.sqlite.gz"
        manifest_tmp = root / "manifest.json"
        _aws("cp", _manifest_uri(uri), str(manifest_tmp), "--only-show-errors")
        manifest = _read_manifest(manifest_tmp)
        archive_uri = str(manifest["archive_uri"])
        if not archive_uri.startswith(uri + ".generations/"):
            raise RuntimeError("independent backup manifest points outside the configured prefix")
        _aws("cp", archive_uri, str(archive_tmp), "--only-show-errors")
        if archive_tmp.stat().st_size != int(manifest["archive_bytes"]):
            raise RuntimeError("independent backup archive size does not match manifest")
        verification = verify_archive(archive_tmp)
        if verification["sha256"] != manifest["sha256"]:
            raise RuntimeError("independent backup archive checksum does not match manifest")
        database = verification["database_verification"]
        if database.get("schema_version") != manifest["schema_version"]:
            raise RuntimeError("independent backup schema does not match manifest")
        summary = _history_summary(archive_tmp)
        if summary["row_counts"] != manifest["row_counts"]:
            raise RuntimeError("independent backup row counts do not match manifest")
        if summary["latest_origin_at"] != manifest["latest_origin_at"]:
            raise RuntimeError("independent backup latest origin does not match manifest")
        os.replace(archive_tmp, destination)
    return manifest, verification


def upload_and_verify(archive: Path | str, uri: str) -> dict[str, Any]:
    """Upload the archive and its row-count manifest, then verify the remote pair."""
    source = Path(archive)
    local = verify_archive(source)
    summary = _history_summary(source)
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "archive_bytes": local["archive_bytes"],
        "sha256": local["sha256"],
        "archive_uri": f"{uri}.generations/{local['sha256']}.sqlite.gz",
        "schema_version": local["database_verification"]["schema_version"],
        **summary,
    }
    with tempfile.TemporaryDirectory(prefix="independent-history-upload-") as directory:
        manifest_path = Path(directory) / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
        # Commit content-addressed data before switching the manifest pointer.
        # Failed uploads leave the previous independent copy usable.
        _aws("cp", str(source), manifest["archive_uri"], "--only-show-errors")
        with tempfile.TemporaryDirectory(prefix="independent-history-verify-") as verify_dir:
            downloaded_archive = Path(verify_dir) / "archive.sqlite.gz"
            _aws("cp", manifest["archive_uri"], str(downloaded_archive), "--only-show-errors")
            remote = verify_archive(downloaded_archive)
            if remote["sha256"] != local["sha256"]:
                raise RuntimeError("independent S3 backup checksum mismatch")
            _aws("cp", str(manifest_path), _manifest_uri(uri), "--only-show-errors")
            downloaded_manifest = Path(verify_dir) / "manifest.json"
            _aws("cp", _manifest_uri(uri), str(downloaded_manifest), "--only-show-errors")
            if downloaded_manifest.read_bytes() != manifest_path.read_bytes():
                raise RuntimeError("independent S3 manifest checksum mismatch")
    return {
        "verified": True,
        **{key: value for key, value in manifest.items() if key != "archive_uri"},
        "database_verification": local["database_verification"],
    }


def restore_from_s3(uri: str, output: Path | str) -> dict[str, Any]:
    """Restore a manifest-matched archive to scratch storage after full verification."""
    manifest, verification = _download_verified(uri, Path(output))
    safe_manifest = {key: value for key, value in manifest.items() if key != "archive_uri"}
    return {**verification, "manifest": safe_manifest, "verified": True}


def record_successful_restore(uri: str) -> dict[str, Any]:
    """Record a successful completed scratch drill in a separate S3 receipt."""
    with tempfile.TemporaryDirectory(prefix="independent-history-receipt-") as directory:
        manifest_path = Path(directory) / "manifest.json"
        receipt_path = Path(directory) / "restore.json"
        _aws("cp", _manifest_uri(uri), str(manifest_path), "--only-show-errors")
        manifest = _read_manifest(manifest_path)
        receipt = {
            "receipt_version": 1,
            "restored_at": datetime.now(timezone.utc).isoformat(),
            "backup_verified_at": manifest["verified_at"],
            "backup_sha256": manifest["sha256"],
        }
        receipt_path.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
        _aws("cp", str(receipt_path), _receipt_uri(uri), "--only-show-errors")
    return receipt


def check_backup(
    uri: str, *, max_age_hours: float = 2.0, now: datetime | None = None
) -> dict[str, Any]:
    """Verify the current remote pair and fail once its age approaches the RPO."""
    if max_age_hours <= 0:
        raise ValueError("max_age_hours must be positive")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="independent-history-check-") as directory:
        root = Path(directory)
        archive = root / "archive.sqlite.gz"
        manifest, verification = _download_verified(uri, archive)
        age = current - datetime.fromisoformat(manifest["verified_at"].replace("Z", "+00:00"))
        if age < timedelta(0):
            raise RuntimeError("independent backup verification timestamp is in the future")
        receipt_path = root / "restore.json"
        last_restore = None
        try:
            _aws("cp", _receipt_uri(uri), str(receipt_path), "--only-show-errors")
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if receipt.get("backup_sha256") == manifest["sha256"]:
                last_restore = receipt.get("restored_at")
        except (RuntimeError, OSError, json.JSONDecodeError):
            pass
    report = {
        "verified": True,
        "checked_at": current.isoformat(),
        "backup_age_seconds": int(age.total_seconds()),
        "max_age_hours": max_age_hours,
        "archive_bytes": manifest["archive_bytes"],
        "sha256": manifest["sha256"],
        "verified_at": manifest["verified_at"],
        "schema_version": verification["database_verification"]["schema_version"],
        "row_counts": manifest["row_counts"],
        "latest_origin_at": manifest["latest_origin_at"],
        "last_successful_restore_at": last_restore,
        "status": "passed",
    }
    if age > timedelta(hours=max_age_hours):
        report["status"] = "failed"
        raise BackupAgeError("independent backup exceeds configured age threshold", report)
    return report


class BackupAgeError(RuntimeError):
    def __init__(self, message: str, report: dict[str, Any]) -> None:
        super().__init__(message)
        self.report = report


def _write_json(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    upload = commands.add_parser("upload")
    upload.add_argument("--archive", type=Path, required=True)
    upload.add_argument("--uri", required=True)
    upload.add_argument("--report", type=Path, required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--uri", required=True)
    restore.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("check")
    check.add_argument("--uri", required=True)
    check.add_argument("--max-age-hours", type=float, default=2.0)
    check.add_argument("--report", type=Path, required=True)
    record = commands.add_parser("record-restore")
    record.add_argument("--uri", required=True)
    args = parser.parse_args()
    if args.command == "upload":
        report = upload_and_verify(args.archive, args.uri)
        _write_json(args.report, report)
    elif args.command == "restore":
        report = restore_from_s3(args.uri, args.output)
    elif args.command == "record-restore":
        report = record_successful_restore(args.uri)
    else:
        try:
            report = check_backup(args.uri, max_age_hours=args.max_age_hours)
        except BackupAgeError as exc:
            report = exc.report
            _write_json(args.report, report)
            print(json.dumps(report, indent=2, sort_keys=True))
            raise SystemExit(2) from exc
        except Exception as exc:
            report = {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "verified": False,
                "status": "failed",
                "failure": str(exc),
            }
            _write_json(args.report, report)
            print(json.dumps(report, indent=2, sort_keys=True))
            raise SystemExit(2) from exc
        _write_json(args.report, report)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
