#!/usr/bin/env python3
"""Copy and verify history archives in an independently administered S3 bucket."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from btc_timesfm.history.history_backup import verify_archive


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
        # AWS diagnostics can include account or endpoint details; never forward them.
        raise RuntimeError("independent S3 backup operation failed") from exc


def upload_and_verify(archive: Path | str, uri: str) -> dict[str, Any]:
    """Upload to an S3 URI, download it again, and verify bytes and database."""
    source = Path(archive)
    local = verify_archive(source)
    _aws("cp", str(source), uri, "--only-show-errors")
    with tempfile.TemporaryDirectory(prefix="independent-history-verify-") as directory:
        downloaded = Path(directory) / "archive.sqlite.gz"
        _aws("cp", uri, str(downloaded), "--only-show-errors")
        independent = verify_archive(downloaded)
        if independent["sha256"] != local["sha256"]:
            raise RuntimeError("independent S3 backup checksum mismatch")
    return {
        "verified": True,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "uri": uri,
        "archive_bytes": local["archive_bytes"],
        "sha256": local["sha256"],
        "database_verification": local["database_verification"],
    }


def restore_from_s3(uri: str, output: Path | str) -> dict[str, Any]:
    """Download independent copy for the existing non-mutating scratch drill."""
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _aws("cp", uri, str(destination), "--only-show-errors")
    return verify_archive(destination)


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
    args = parser.parse_args()
    if args.command == "upload":
        report = upload_and_verify(args.archive, args.uri)
        args.report.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    else:
        report = restore_from_s3(args.uri, args.output)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
