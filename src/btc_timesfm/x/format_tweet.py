#!/usr/bin/env python3
"""Render the direction-first B1 X post from forecast.json."""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

FORECAST_PATH = Path("forecast.json")
TWEET_PATH = Path("tweet.txt")
HORIZONS = ("2h", "4h", "8h", "16h")


def format_price(value: float) -> str:
    return f"${value:,.0f}"


def direction_icon(change_pct: float) -> str:
    if change_pct > 0.005:
        return "↗"
    if change_pct < -0.005:
        return "↘"
    return "→"


def direction_signal(change_pct: float) -> tuple[str, str]:
    """Return the visual signal used by the B1 horizon rows."""
    if change_pct > 0.005:
        return "🟢", "UP"
    if change_pct < -0.005:
        return "🔴", "DOWN"
    return "⚪", "FLAT"


def horizon_text(horizon: str, prediction: dict[str, Any]) -> str:
    """Retain the legacy horizon formatter for callers outside the tweet layout."""
    change = float(prediction["change_pct"])
    return (
        f"{horizon} {direction_icon(change)} {format_price(float(prediction['price_usd']))} "
        f"({change:+.2f}%)"
    )


def _format_number(value: float, decimals: int = 2) -> str:
    """Format signed percentages compactly, including pathological large values."""
    if not math.isfinite(value):
        return f"{value:+g}"
    if abs(value) >= 1000:
        return f"{value:+.1e}"
    return f"{value:+.{decimals}f}"


def format_move(value: float, decimals: int = 2) -> str:
    return f"{_format_number(value, decimals)}%"


def format_delta(value: float, decimals: int = 2) -> str:
    return f"{_format_number(value, decimals)}pp"


def previous_outcome_text(score: dict[str, Any] | None, *, compact: bool = False) -> str:
    """Render previous predicted move, actual move and actual-predicted delta."""
    if not score:
        return "Prev …" if not compact else "…"

    try:
        predicted = float(score["predicted_change_pct"])
        actual = float(score["actual_change_pct"])
    except (KeyError, TypeError, ValueError):
        return "Prev …" if not compact else "…"

    delta = actual - predicted
    direction_correct = score.get("direction_correct")
    if direction_correct is None:
        direction_correct = (
            (predicted > 0) == (actual > 0) if predicted and actual else predicted == actual
        )
    mark = "✅" if bool(direction_correct) else "❌"
    decimals = 1 if compact else 2

    if compact:
        return (
            f"{mark} P{format_move(predicted, decimals)} "
            f"A{format_move(actual, decimals)} Δ{format_delta(delta, decimals)}"
        )
    return (
        f"Prev {mark} P{format_move(predicted, decimals)} "
        f"A{format_move(actual, decimals)} Δ{format_delta(delta, decimals)}"
    )


def signal_row(
    horizon: str,
    prediction: dict[str, Any],
    score: dict[str, Any] | None,
    *,
    compact: bool = False,
) -> str:
    change = float(prediction["change_pct"])
    emoji, label = direction_signal(change)
    decimals = 1 if compact else 2
    current = format_move(change, decimals)

    if compact:
        return f"{horizon} {emoji}{current} | {previous_outcome_text(score, compact=True)}"
    return f"{horizon} {emoji} {label} {current} | {previous_outcome_text(score)}"


def consensus_text(predictions: dict[str, Any]) -> str:
    labels = [direction_signal(float(predictions[h]["change_pct"]))[1] for h in HORIZONS]
    counts = Counter(labels)
    best_count = max(counts.values())
    winners = [label for label, count in counts.items() if count == best_count]

    if len(winners) != 1:
        return "🤝 mixed outlook"

    label = winners[0]
    description = {"UP": "bullish", "DOWN": "bearish", "FLAT": "neutral"}[label]
    return f"🤝 {best_count}/4 {description}"


def confidence_text(output: dict[str, Any], *, compact: bool = False) -> str | None:
    """Render an evidence band only when all four horizons have enough OOS support."""
    confidence = output.get("forecast_confidence")
    if not isinstance(confidence, dict) or confidence.get("status") != "available":
        return None

    label = str(confidence.get("label") or "").lower()
    if label not in {"low", "moderate", "high"}:
        return None
    try:
        samples = int(confidence["minimum_evidence_samples"])
        edge = float(confidence["minimum_edge_vs_persistence_pct"])
    except (KeyError, TypeError, ValueError):
        return None
    if samples < 1 or not math.isfinite(edge):
        return None

    short_label = {"low": "LOW", "moderate": "MOD", "high": "HIGH"}[label]
    if compact:
        return f"📊 Conf {short_label} • edge≥{edge:+.0f}% • n≥{samples}"
    return f"📊 Confidence {short_label} • min edge {edge:+.1f}% vs persistence • n≥{samples}"


def _render_tweet(
    output: dict[str, Any],
    *,
    compact: bool,
    include_confidence: bool,
    compact_confidence: bool,
    include_price: bool,
) -> str:
    predictions = output["predictions"]
    reliability = output.get("forecast_reliability", {})

    lines = ["₿ BTC SIGNAL"]
    lines.extend(
        signal_row(horizon, predictions[horizon], reliability.get(horizon), compact=compact)
        for horizon in HORIZONS
    )
    if include_confidence:
        confidence = confidence_text(output, compact=compact_confidence)
        if confidence:
            lines.append(confidence)

    if include_price:
        lines.append(f"💰 {format_price(float(output['latest_close_usd']))} • ⚠️ Experimental • NFA")
    else:
        lines.append("⚠️ Experimental • NFA")
    return "\n".join(lines)


def build_visual_tweet(output: dict[str, Any]) -> str:
    """Build the selected B1 direction-first template with deterministic fallbacks."""
    attempts = (
        dict(compact=False, include_confidence=True, compact_confidence=False, include_price=True),
        dict(compact=False, include_confidence=True, compact_confidence=True, include_price=True),
        dict(compact=False, include_confidence=True, compact_confidence=True, include_price=False),
        dict(compact=True, include_confidence=True, compact_confidence=True, include_price=False),
        dict(compact=True, include_confidence=False, compact_confidence=True, include_price=False),
    )

    for options in attempts:
        tweet = _render_tweet(output, **options)
        if len(tweet) <= 280:
            return tweet

    raise RuntimeError("Generated X post is too long even after compact fallback")


def main() -> None:
    output = json.loads(FORECAST_PATH.read_text(encoding="utf-8"))
    tweet = build_visual_tweet(output)
    TWEET_PATH.write_text(tweet + "\n", encoding="utf-8")
    print(f"Refreshed {TWEET_PATH} ({len(tweet)} characters):\n\n{tweet}")


if __name__ == "__main__":
    main()
