#!/usr/bin/env python3
"""Create, verify, restore, and prune durable forecast-history backups."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from history_migrations import migrate_database, validate_database

BACKUP_PREFIX = "forecast_history.backup-"
BACKUP_SUFFIX = ".sqlite.gz"
DEFAULT_KEEP_GENERATIONS = 7
DEFAULT_MAX_GENERATION_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_TOTAL_BACKUP_BYTES = 250 * 1024 * 1024


def _verification_ok(result: dict[str, Any]) -> bool:
    return (
        result.get("integrity") == "ok"
        and int(result.get("foreign_key_violations", 1)) == 0
        and result.get("schema_version") == result.get("supported_schema_version")
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_asset_name(generation: str) -> str:
    """Return the versioned release-asset name for one backup generation."""
    generation = generation.strip()
    if not generation or any(not (char.isalnum() or char in "-_.") for char in generation):
        raise ValueError("generation must contain only letters, digits, '-', '_' or '.'")
    return f"{BACKUP_PREFIX}{generation}{BACKUP_SUFFIX}"


def create_archive(
    source_db: Path | str,
    output_archive: Path | str,
    *,
    max_generation_bytes: int = DEFAULT_MAX_GENERATION_BYTES,
) -> dict[str, Any]:
    """Validate and gzip a database without modifying the source."""
    source = Path(source_db)
    output = Path(output_archive)
    if max_generation_bytes < 1:
        raise ValueError("max_generation_bytes must be positive")
    if not source.is_file():
        raise FileNotFoundError(source)

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        # A previous-known-good database can legitimately be one schema version
        # behind immediately after a new migration ships. Prepare the backup from
        # a temporary copy so it is upgraded to the current schema without
        # mutating the rollback source itself.
        with tempfile.TemporaryDirectory(prefix="forecast-history-backup-") as directory:
            prepared = Path(directory) / "forecast_history.sqlite"
            shutil.copy2(source, prepared)
            migrate_database(prepared)
            verification = validate_database(prepared)
            if not _verification_ok(verification):
                raise RuntimeError(f"source database failed verification: {verification}")

            with (
                prepared.open("rb") as source_handle,
                gzip.open(temporary, "wb", compresslevel=9) as target,
            ):
                shutil.copyfileobj(source_handle, target, length=1024 * 1024)

        archive_bytes = temporary.stat().st_size
        if archive_bytes > max_generation_bytes:
            raise RuntimeError(
                f"compressed backup is {archive_bytes} bytes, exceeding "
                f"the {max_generation_bytes}-byte generation limit"
            )
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)

    return {
        "archive": str(output),
        "archive_bytes": output.stat().st_size,
        "sha256": _sha256(output),
        "database_verification": {**verification, "ok": True},
    }


def verify_archive(
    archive_path: Path | str,
    *,
    max_generation_bytes: int = DEFAULT_MAX_GENERATION_BYTES,
) -> dict[str, Any]:
    """Decompress an archive to a temporary file and validate the contained DB."""
    archive = Path(archive_path)
    if max_generation_bytes < 1:
        raise ValueError("max_generation_bytes must be positive")
    if not archive.is_file():
        raise FileNotFoundError(archive)

    archive_bytes = archive.stat().st_size
    if archive_bytes > max_generation_bytes:
        raise RuntimeError(
            f"compressed backup is {archive_bytes} bytes, exceeding "
            f"the {max_generation_bytes}-byte generation limit"
        )

    with tempfile.TemporaryDirectory(prefix="forecast-history-verify-") as directory:
        restored = Path(directory) / "forecast_history.sqlite"
        try:
            with gzip.open(archive, "rb") as source, restored.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
        except (OSError, EOFError) as exc:
            raise RuntimeError(f"backup archive cannot be decompressed: {archive}") from exc

        verification = validate_database(restored)
        if not _verification_ok(verification):
            raise RuntimeError(f"backup database failed verification: {verification}")

    return {
        "archive": str(archive),
        "archive_bytes": archive_bytes,
        "sha256": _sha256(archive),
        "database_verification": {**verification, "ok": True},
    }


def restore_archive(
    archive_path: Path | str,
    output_db: Path | str,
    *,
    max_generation_bytes: int = DEFAULT_MAX_GENERATION_BYTES,
) -> dict[str, Any]:
    """Atomically restore a verified archive to ``output_db``."""
    archive = Path(archive_path)
    output = Path(output_db)
    archive_report = verify_archive(archive, max_generation_bytes=max_generation_bytes)

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.restore.tmp")
    try:
        try:
            with gzip.open(archive, "rb") as source, temporary.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
        except (OSError, EOFError) as exc:
            raise RuntimeError(f"backup archive cannot be decompressed: {archive}") from exc

        verification = validate_database(temporary)
        if not _verification_ok(verification):
            raise RuntimeError(f"restored database failed verification: {verification}")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)

    return {
        **archive_report,
        "restored_to": str(output),
        "restored_bytes": output.stat().st_size,
        "restored_database_verification": {**verification, "ok": True},
    }


def _is_backup_asset(asset: dict[str, Any]) -> bool:
    name = str(asset.get("name") or "")
    return name.startswith(BACKUP_PREFIX) and name.endswith(BACKUP_SUFFIX)


def retention_plan(
    assets: list[dict[str, Any]],
    *,
    keep_generations: int = DEFAULT_KEEP_GENERATIONS,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BACKUP_BYTES,
) -> dict[str, Any]:
    """Choose versioned backup assets to keep/delete under bounded retention."""
    if keep_generations < 1:
        raise ValueError("keep_generations must be >= 1")
    if max_total_bytes < 1:
        raise ValueError("max_total_bytes must be positive")

    backups = [dict(asset) for asset in assets if _is_backup_asset(asset)]
    backups.sort(
        key=lambda asset: (str(asset.get("created_at") or ""), str(asset.get("name") or "")),
        reverse=True,
    )

    keep: list[dict[str, Any]] = []
    delete: list[dict[str, Any]] = []
    kept_bytes = 0

    for index, asset in enumerate(backups):
        try:
            size = max(0, int(asset.get("size") or 0))
        except (TypeError, ValueError):
            size = 0

        # Always retain the newest generation. This guarantees at least one
        # known-good rollback point even if it alone crosses the byte budget.
        if index == 0:
            keep.append(asset)
            kept_bytes += size
            continue

        within_count = len(keep) < keep_generations
        within_bytes = kept_bytes + size <= max_total_bytes
        if within_count and within_bytes:
            keep.append(asset)
            kept_bytes += size
        else:
            delete.append(asset)

    return {
        "policy": {
            "keep_generations": keep_generations,
            "max_total_bytes": max_total_bytes,
        },
        "backup_assets_seen": len(backups),
        "kept_bytes": kept_bytes,
        "keep": keep,
        "delete": delete,
    }


def _write_json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manage durable forecast-history backup generations"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--source", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_GENERATION_BYTES)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--archive", type=Path, required=True)
    verify.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_GENERATION_BYTES)

    restore = subparsers.add_parser("restore")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--output", type=Path, required=True)
    restore.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_GENERATION_BYTES)

    retention = subparsers.add_parser("retention-plan")
    retention.add_argument("--assets-json", type=Path, required=True)
    retention.add_argument("--keep", type=int, default=DEFAULT_KEEP_GENERATIONS)
    retention.add_argument("--max-total-bytes", type=int, default=DEFAULT_MAX_TOTAL_BACKUP_BYTES)

    args = parser.parse_args()
    if args.command == "create":
        _write_json(create_archive(args.source, args.output, max_generation_bytes=args.max_bytes))
    elif args.command == "verify":
        _write_json(verify_archive(args.archive, max_generation_bytes=args.max_bytes))
    elif args.command == "restore":
        _write_json(restore_archive(args.archive, args.output, max_generation_bytes=args.max_bytes))
    elif args.command == "retention-plan":
        payload = json.loads(args.assets_json.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("assets JSON must contain a list")
        assets = [dict(item) for item in payload if isinstance(item, dict)]
        _write_json(
            retention_plan(
                assets,
                keep_generations=args.keep,
                max_total_bytes=args.max_total_bytes,
            )
        )


if __name__ == "__main__":
    main()
