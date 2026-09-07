#!/usr/bin/env python3
"""Decide whether a scheduled forecast is due.

GitHub Actions cron runs are best-effort and may be delayed or dropped. The
workflow wakes hourly and this guard compares completed BTC candle timestamps,
so production forecasting is independent of X/Twitter publication success.

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
MIN_FORECAST_GAP_HOURS = 1


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
        snapshots = [state]

    timestamps = [
        parsed
        for snapshot in snapshots
        if (parsed := parse_timestamp(snapshot.get("latest_close_at"))) is not None
    ]
    return max(timestamps) if timestamps else None


def latest_successful_x_post(registry_path: Path) -> datetime | None:
    """Retained for compatibility and diagnostics; it no longer controls cadence."""
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
    # Keep x_post_registry_path in the public signature for compatibility with
    # existing callers. X state is intentionally not used to decide cadence.
    _ = x_post_registry_path

    if event_name != "schedule":
        return (
            True,
            "Manual/non-scheduled run: forecast will run without controlling schedule cadence.",
        )

    checked_at = now or datetime.now(timezone.utc)
    current_hour = completed_hour(checked_at)
    last_forecast = latest_forecast_close(state_path)
    if last_forecast is None:
        return True, "No prior forecast candle found: forecast will run."

    gap_hours = (current_hour - completed_hour(last_forecast)).total_seconds() / 3600.0
    if gap_hours >= MIN_FORECAST_GAP_HOURS:
        return (
            True,
            f"Forecast is due: {gap_hours:.1f} completed candle hours since the last forecast "
            f"at {last_forecast.isoformat()}.",
        )

    return (
        False,
        f"Forecast not due yet: latest forecast is {last_forecast.isoformat()} and the current "
        f"completed candle is {current_hour.isoformat()} (need {MIN_FORECAST_GAP_HOURS}h).",
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
