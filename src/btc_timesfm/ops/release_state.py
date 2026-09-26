#!/usr/bin/env python3
"""Verified persistence for small stateful GitHub Release assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Callable


class ReleaseStateError(RuntimeError):
    """An expected Release state could not be safely persisted or restored."""


def persist_assets(
    assets: list[Path],
    *,
    release_tag: str,
    repository: str,
    run_id: str,
    attempts: int = 3,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Upload assets with bounded retries and verify exact bytes via Release read-back."""
    if attempts < 1:
        raise ValueError("attempts must be positive")
    for asset in assets:
        expected = hashlib.sha256(asset.read_bytes()).hexdigest()
        failure_class = "api_error"
        for attempt in range(1, attempts + 1):
            try:
                run(
                    [
                        "gh",
                        "release",
                        "upload",
                        release_tag,
                        str(asset),
                        "--repo",
                        repository,
                        "--clobber",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                target = asset.parent / "release-state-verify"
                target.mkdir(exist_ok=True)
                (target / asset.name).unlink(missing_ok=True)
                run(
                    [
                        "gh",
                        "release",
                        "download",
                        release_tag,
                        "--repo",
                        repository,
                        "--pattern",
                        asset.name,
                        "--dir",
                        str(target),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                actual = hashlib.sha256((target / asset.name).read_bytes()).hexdigest()
                if actual != expected:
                    failure_class = "digest_mismatch"
                    raise ReleaseStateError("Release asset read-back digest mismatch")
                print(f"Verified release state run_id={run_id} asset={asset.name} attempts={attempt}")
                break
            except (OSError, subprocess.SubprocessError, ReleaseStateError) as exc:
                # Do not log subprocess output or exception text: gh diagnostics may contain sensitive data.
                if isinstance(exc, ReleaseStateError):
                    failure_class = "digest_mismatch"
                elif isinstance(exc, OSError):
                    failure_class = "transport_error"
                else:
                    failure_class = "api_error"
                print(
                    f"Release state failure run_id={run_id} asset={asset.name} "
                    f"attempts={attempt} failure_class={failure_class}"
                )
                if attempt == attempts:
                    raise ReleaseStateError(
                        f"Release state persistence exhausted for {asset.name} "
                        f"(run_id={run_id}, attempts={attempts}, failure_class={failure_class})"
                    ) from None
                sleep(min(2**(attempt - 1), 8))


def validate_restored_state(paths: list[Path], *, release_exists: bool) -> None:
    """Allow missing state only before the release exists; validate present JSON state."""
    if not release_exists:
        return
    for path in paths:
        if not path.is_file():
            raise ReleaseStateError(
                f"Existing history release is missing required state asset: {path.name}"
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise ReleaseStateError(f"Restored state asset is corrupt: {path.name}") from None
        if not isinstance(payload, dict):
            raise ReleaseStateError(f"Restored state asset is invalid: {path.name}")
        if path.name == "x_post_registry.json" and (
            payload.get("version") != 1 or not isinstance(payload.get("posts"), dict)
        ):
            raise ReleaseStateError("Restored X registry is invalid")
        if path.name == "pipeline_health.json" and (
            payload.get("version") != 1
            or not isinstance(payload.get("stages"), dict)
            or not isinstance(payload.get("current_signals"), dict)
        ):
            raise ReleaseStateError("Restored pipeline health state is invalid")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    persist = subparsers.add_parser("persist")
    persist.add_argument("--release", required=True)
    persist.add_argument("--repository", default=os.getenv("GITHUB_REPOSITORY", ""))
    persist.add_argument("--run-id", default=os.getenv("GITHUB_RUN_ID", "unknown"))
    persist.add_argument("--attempts", type=int, default=3)
    persist.add_argument("assets", nargs="+", type=Path)
    args = parser.parse_args()
    persist_assets(
        args.assets,
        release_tag=args.release,
        repository=args.repository,
        run_id=args.run_id,
        attempts=args.attempts,
    )


if __name__ == "__main__":
    main()
