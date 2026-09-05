#!/usr/bin/env python3
"""Render a compact, emoji-rich X post from forecast.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FORECAST_PATH = Path("forecast.json")
TWEET_PATH = Path("tweet.txt")

REGIME_LABELS = {
    "range": "↔️ Range",
    "trending": "📈 Trending",
    "high_volatility": "⚡ High volatility",
}


def format_price(value: float) -> str:
    return f"${value:,.0f}"


def direction_icon(change_pct: float) -> str:
    if change_pct > 0.005:
        return "↗"
    if change_pct < -0.005:
        return "↘"
    return "→"


def horizon_text(horizon: str, prediction: dict[str, Any]) -> str:
    change = float(prediction["change_pct"])
    return (
        f"{horizon} {direction_icon(change)} {format_price(float(prediction['price_usd']))} "
        f"({change:+.2f}%)"
    )


def build_visual_tweet(output: dict[str, Any]) -> str:
    predictions = output["predictions"]
    reliability = output.get("forecast_reliability", {})
    regime = str(output.get("regime", "range"))
    regime_text = REGIME_LABELS.get(regime, f"🧭 {regime.replace('_', ' ').title()}")

    agreements = [
        float(predictions[h].get("model_agreement", 0.0))
        for h in ("2h", "4h", "8h", "16h")
        if h in predictions
    ]
    average_agreement = sum(agreements) / len(agreements) if agreements else 0.0

    errors: list[str] = []
    for horizon in ("2h", "4h", "8h", "16h"):
        score = reliability.get(horizon)
        errors.append(
            f"{horizon} {float(score['absolute_error_pct']):.2f}%" if score else f"{horizon} —"
        )

    lines = [
        "₿ BTC/USD • Ensemble",
        f"💰 Now {format_price(float(output['latest_close_usd']))} • {regime_text}",
        "⏱ " + " | ".join(horizon_text(h, predictions[h]) for h in ("2h", "4h")),
        "🔭 " + " | ".join(horizon_text(h, predictions[h]) for h in ("8h", "16h")),
        f"🤝 Models {average_agreement * 100:.0f}% agree",
        "🎯 Error: " + " | ".join(errors),
        "⚠️ Experimental • NFA",
    ]

    tweet = "\n".join(lines)

    # Keep graceful fallbacks in case larger BTC prices or future labels make the post longer.
    if len(tweet) > 280:
        lines.pop(4)  # remove model-agreement line first
        tweet = "\n".join(lines)
    if len(tweet) > 280:
        lines[1] = f"💰 {format_price(float(output['latest_close_usd']))} • {regime_text}"
        tweet = "\n".join(lines)
    if len(tweet) > 280:
        lines[-2] = "🎯 Error: " + " | ".join(errors[:2])
        tweet = "\n".join(lines)
    if len(tweet) > 280:
        raise RuntimeError(f"Generated X post is too long: {len(tweet)} characters")

    return tweet


def main() -> None:
    output = json.loads(FORECAST_PATH.read_text(encoding="utf-8"))
    tweet = build_visual_tweet(output)
    TWEET_PATH.write_text(tweet + "\n", encoding="utf-8")
    print(f"Refreshed {TWEET_PATH} ({len(tweet)} characters):\n\n{tweet}")


if __name__ == "__main__":
    main()
