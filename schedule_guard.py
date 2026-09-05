#!/usr/bin/env python3
"""Decide whether a scheduled forecast is due.

GitHub Actions cron runs are best-effort and may be delayed or dropped. The
workflow therefore wakes up hourly, restores the latest forecast history, and
uses this guard to run the expensive forecast only when at least two completed
hourly candles have elapsed since the last saved forecast.

Manual workflow_dispatch runs always proceed.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_PATH = Path(".state/previous_forecast.json")
MIN_CANDLE_GAP_HOURS = 2


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


def completed_hour(now: datetime) -> datetime:
    now = now.astimezone(timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0)


def should_run(
    event_name: str,
    state_path: Path = STATE_PATH,
    now: datetime | None = None,
) -> tuple[bool, str]:
    if event_name != "schedule":
        return True, "Manual/non-scheduled run: forecast will run."

    last_close = latest_forecast_close(state_path)
    if last_close is None:
        return True, "No usable forecast history found: forecast will run."

    current_close = completed_hour(now or datetime.now(timezone.utc))
    candle_gap_hours = (current_close - last_close).total_seconds() / 3600.0

    if candle_gap_hours >= MIN_CANDLE_GAP_HOURS:
        return (
            True,
            f"Forecast is due: {candle_gap_hours:.1f} completed candle hours since "
            f"{last_close.isoformat()}.",
        )

    return (
        False,
        f"Forecast not due yet: {candle_gap_hours:.1f} completed candle hours since "
        f"{last_close.isoformat()} (need {MIN_CANDLE_GAP_HOURS}).",
    )


def main() -> None:
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    state_path = Path(os.environ.get("FORECAST_STATE_PATH", str(STATE_PATH)))
    run_forecast, reason = should_run(event_name, state_path)

    print(reason)

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with Path(github_output).open("a", encoding="utf-8") as output:
            output.write(f"run_forecast={'true' if run_forecast else 'false'}\n")
            output.write(f"reason={reason}\n")


if __name__ == "__main__":
    main()
