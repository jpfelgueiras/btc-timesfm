"""Research-only publication-time price matching and position-overlap rules."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any


def _utc(value: str | datetime) -> datetime:
    parsed = (
        datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    )
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def executable_entry(
    generated_at: str | datetime,
    target_at: str | datetime,
    prices_by_timestamp: dict[str | datetime, float],
) -> dict[str, Any] | None:
    """Select the first known price at/after publication and before target.

    No candle close at the forecast origin is used as a fill after generation
    has occurred. If no eligible timestamped price exists, return ``None``.
    """
    issued = _utc(generated_at)
    target = _utc(target_at)
    if target <= issued:
        return None
    eligible: list[tuple[datetime, float]] = []
    for timestamp, raw_price in prices_by_timestamp.items():
        at = _utc(timestamp)
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            continue
        if at >= issued and at < target and math.isfinite(price) and price > 0.0:
            eligible.append((at, price))
    if not eligible:
        return None
    entry_at, price = min(eligible, key=lambda item: item[0])
    return {"entry_at": entry_at.isoformat(), "entry_price_usd": price}


def select_non_overlapping_positions(signals: list[dict[str, Any]]) -> dict[str, Any]:
    """Greedily retain first-issued opportunities until their target time.

    Signals must contain timezone-aware ``entry_at`` and ``target_at``. This
    fixed first-come rule is a transparent no-netting baseline, not a trading
    recommendation or fitted policy.
    """
    ordered = sorted(signals, key=lambda item: _utc(item["entry_at"]))
    retained: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    last_exit: datetime | None = None
    for signal in ordered:
        entry = _utc(signal["entry_at"])
        target = _utc(signal["target_at"])
        if target <= entry:
            skipped.append({**signal, "skip_reason": "target_not_after_entry"})
        elif last_exit is not None and entry < last_exit:
            skipped.append({**signal, "skip_reason": "position_already_open"})
        else:
            retained.append(signal)
            last_exit = target
    return {"retained": retained, "skipped": skipped}
