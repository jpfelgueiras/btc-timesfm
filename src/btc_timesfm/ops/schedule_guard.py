#!/usr/bin/env python3
"""Decide whether a scheduled forecast is due.

GitHub Actions cron runs are best-effort and may be delayed or dropped. The
workflow therefore wakes up on a two-hour cadence, restores durable X posting
state, and uses this guard to run the expensive forecast only when at least two
hours have elapsed since the last successful X publication.

Manual workflow_dispatch runs always proceed.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_PATH = Path(".state/previous_forecast.json")
X_POST_REGISTRY_PATH = Path(".state/x_post_registry.json")
MIN_POST_GAP_HOURS = 2


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def latest_forecast_close(state_path: Path) -> datetime | None:
    if not state_path.exists():
        return None

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    snapshots: list[dict[str, Any]] = []
    if isinstance(state, dict) and isinstance(state.get("forecasts"), list):
        snapshots = [item for item in state["forecasts"] if isinstance(item, dict)]
    elif isinstance(state, dict) and "predictions" in state:
        # Legacy single-forecast cache format.
        snapshots = [state]

    timestamps = [
        parsed
        for snapshot in snapshots
        if (parsed := parse_timestamp(snapshot.get("latest_close_at"))) is not None
    ]
    return max(timestamps) if timestamps else None


def latest_successful_x_post(registry_path: Path) -> datetime | None:
    if not registry_path.exists():
        return None

    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    if not isinstance(registry, dict):
        return None

    timestamps = []
    root_timestamp = parse_timestamp(registry.get("last_successful_x_post_at"))
    if root_timestamp is not None:
        timestamps.append(root_timestamp)

    posts = registry.get("posts")
    if isinstance(posts, dict):
        timestamps.extend(
            parsed
            for item in posts.values()
            if isinstance(item, dict)
            and item.get("status") == "posted"
            and (parsed := parse_timestamp(item.get("posted_at"))) is not None
        )

    return max(timestamps) if timestamps else None


def completed_hour(now: datetime) -> datetime:
    now = now.astimezone(timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0)


def should_run(
    event_name: str,
    state_path: Path = STATE_PATH,
    x_post_registry_path: Path = X_POST_REGISTRY_PATH,
    now: datetime | None = None,
) -> tuple[bool, str]:
    # Keep state_path in the public signature for compatibility with older tests
    # and local callers; scheduled X cadence is now controlled by the registry.
    _ = state_path

    if event_name != "schedule":
        return (
            True,
            "Manual/non-scheduled run: forecast will run without controlling schedule cadence.",
        )

    checked_at = now or datetime.now(timezone.utc)
    last_post = latest_successful_x_post(x_post_registry_path)
    if last_post is None:
        return True, "No successful X publication history found: forecast will run."

    post_gap_hours = (checked_at.astimezone(timezone.utc) - last_post).total_seconds() / 3600.0

    if post_gap_hours >= MIN_POST_GAP_HOURS:
        return (
            True,
            f"Forecast is due: {post_gap_hours:.1f} hours since the last successful X post "
            f"at {last_post.isoformat()}.",
        )

    return (
        False,
        f"Forecast not due yet: {post_gap_hours:.1f} hours since the last successful X post "
        f"at {last_post.isoformat()} (need {MIN_POST_GAP_HOURS}).",
    )


def main() -> None:
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    state_path = Path(os.environ.get("FORECAST_STATE_PATH", str(STATE_PATH)))
    x_post_registry_path = Path(os.environ.get("X_POST_REGISTRY_PATH", str(X_POST_REGISTRY_PATH)))
    run_forecast, reason = should_run(event_name, state_path, x_post_registry_path)

    print(reason)

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with Path(github_output).open("a", encoding="utf-8") as output:
            output.write(f"run_forecast={'true' if run_forecast else 'false'}\n")
            output.write(f"reason={reason}\n")


if __name__ == "__main__":
    main()
