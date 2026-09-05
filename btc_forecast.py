#!/usr/bin/env python3
"""Forecast BTC/USD with TimesFM 3 and score matured multi-horizon predictions."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import requests
from timesfm3 import ModelConfig, TimesFM3Evaluator


KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
PAIR = "XBTUSD"
INTERVAL_MINUTES = 60

CONTEXT_POINTS = 512
FORECAST_HOURS = 16
TARGET_HOURS = (2, 4, 8, 16)
HISTORY_LIMIT = 48

MODEL_ID = "google/timesfm-3.0-pytorch"
OUTPUT_PATH = Path("forecast.json")
TWEET_PATH = Path("tweet.txt")
STATE_PATH = Path(".state/previous_forecast.json")


def get_completed_hourly_candles() -> tuple[np.ndarray, list[int]]:
    response = requests.get(
        KRAKEN_OHLC_URL,
        params={"pair": PAIR, "interval": INTERVAL_MINUTES},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()

    if payload.get("error"):
        raise RuntimeError(f"Kraken API error: {payload['error']}")

    result = payload["result"]
    pair_key = next(key for key in result if key != "last")
    candles = result[pair_key]

    now = time.time()
    completed = [
        candle
        for candle in candles
        if float(candle[0]) + INTERVAL_MINUTES * 60 <= now
    ]

    if len(completed) < 64:
        raise RuntimeError(f"Not enough completed candles: {len(completed)}")

    completed = completed[-CONTEXT_POINTS:]
    closes = np.asarray([float(candle[4]) for candle in completed], dtype=np.float32)
    close_timestamps = [int(float(candle[0])) + INTERVAL_MINUTES * 60 for candle in completed]
    return closes, close_timestamps


def load_model() -> TimesFM3Evaluator:
    print(f"Loading {MODEL_ID} on CPU...")
    config = ModelConfig(
        checkpoint_path=MODEL_ID,
        per_core_batch_size=1,
        device="cpu",
    )
    return TimesFM3Evaluator(config)


def load_forecast_history() -> list[dict[str, Any]]:
    """Load forecast history, including the legacy single-forecast state format."""
    if not STATE_PATH.exists():
        return []

    try:
        state = json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Ignoring unreadable forecast state: {exc}")
        return []

    if isinstance(state, dict) and isinstance(state.get("forecasts"), list):
        return [item for item in state["forecasts"] if isinstance(item, dict)]

    # Backwards compatibility with the old cache, which stored one forecast directly.
    if isinstance(state, dict) and "predictions" in state:
        return [state]

    print("Ignoring incompatible forecast state format")
    return []


def score_prediction(
    forecast_snapshot: dict[str, Any],
    hour: int,
    actual_by_timestamp: dict[int, float],
) -> dict[str, Any] | None:
    try:
        previous_close = float(forecast_snapshot["latest_close_usd"])
        previous_close_at = datetime.fromisoformat(forecast_snapshot["latest_close_at"])
        prediction = forecast_snapshot["predictions"][f"{hour}h"]
        predicted_price = float(prediction["price_usd"])
        q10 = float(prediction["q10_usd"])
        q90 = float(prediction["q90_usd"])
    except (KeyError, TypeError, ValueError):
        return None

    if previous_close_at.tzinfo is None:
        previous_close_at = previous_close_at.replace(tzinfo=timezone.utc)

    origin_at = previous_close_at.astimezone(timezone.utc)
    target_at = origin_at + timedelta(hours=hour)
    actual_price = actual_by_timestamp.get(int(target_at.timestamp()))
    if actual_price is None:
        return None

    absolute_error_usd = abs(predicted_price - actual_price)
    absolute_error_pct = absolute_error_usd / actual_price * 100.0
    predicted_change_pct = (predicted_price / previous_close - 1.0) * 100.0
    actual_change_pct = (actual_price / previous_close - 1.0) * 100.0

    def direction(value: float, epsilon: float = 1e-9) -> int:
        if value > epsilon:
            return 1
        if value < -epsilon:
            return -1
        return 0

    return {
        "horizon": f"{hour}h",
        "forecast_origin_at": origin_at.isoformat(),
        "target_at": target_at.isoformat(),
        "predicted_price_usd": round(predicted_price, 2),
        "actual_price_usd": round(actual_price, 2),
        "absolute_error_usd": round(absolute_error_usd, 2),
        "absolute_error_pct": round(absolute_error_pct, 4),
        "predicted_change_pct": round(predicted_change_pct, 4),
        "actual_change_pct": round(actual_change_pct, 4),
        "direction_correct": direction(predicted_price - previous_close)
        == direction(actual_price - previous_close),
        "within_q10_q90": q10 <= actual_price <= q90,
    }


def score_forecast_history(
    history: list[dict[str, Any]],
    closes: np.ndarray,
    close_timestamps: list[int],
) -> dict[str, dict[str, Any]]:
    """Score the most recent matured forecast for each configured horizon."""
    actual_by_timestamp = dict(zip(close_timestamps, map(float, closes), strict=True))
    reliability: dict[str, dict[str, Any]] = {}

    for hour in TARGET_HOURS:
        for snapshot in reversed(history):
            score = score_prediction(snapshot, hour, actual_by_timestamp)
            if score is not None:
                reliability[f"{hour}h"] = score
                break

    return reliability


def save_forecast_history(history: list[dict[str, Any]], output: dict[str, Any]) -> None:
    """Persist compact forecast snapshots for future 2h/4h/8h/16h scoring."""
    snapshot = {
        "generated_at": output["generated_at"],
        "latest_close_at": output["latest_close_at"],
        "latest_close_usd": output["latest_close_usd"],
        "predictions": output["predictions"],
    }

    # Replace any forecast for the same source candle, which can happen with manual reruns.
    deduplicated = [
        item
        for item in history
        if item.get("latest_close_at") != snapshot["latest_close_at"]
    ]
    deduplicated.append(snapshot)
    deduplicated = deduplicated[-HISTORY_LIMIT:]

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps({"version": 2, "forecasts": deduplicated}, indent=2) + "\n"
    )


def format_price(value: float) -> str:
    return f"${value:,.0f}"


def build_tweet(output: dict[str, Any]) -> str:
    predictions = output["predictions"]
    reliability = output.get("forecast_reliability", {})

    lines = [
        "BTC/USD - TimesFM 3",
        f"Now {format_price(output['latest_close_usd'])}",
        " | ".join(
            f"{h} {format_price(predictions[h]['price_usd'])} ({predictions[h]['change_pct']:+.2f}%)"
            for h in ("2h", "4h")
        ),
        " | ".join(
            f"{h} {format_price(predictions[h]['price_usd'])} ({predictions[h]['change_pct']:+.2f}%)"
            for h in ("8h", "16h")
        ),
    ]

    error_parts = []
    for horizon in ("2h", "4h", "8h", "16h"):
        score = reliability.get(horizon)
        error_parts.append(
            f"{horizon} {score['absolute_error_pct']:.2f}%" if score else f"{horizon} --"
        )
    lines.append("Prev err: " + " | ".join(error_parts))
    lines.append("Experimental - not financial advice.")

    tweet = "\n".join(lines)
    if len(tweet) > 280:
        raise RuntimeError(f"Generated X post is too long: {len(tweet)} characters")
    return tweet


def print_reliability(reliability: dict[str, dict[str, Any]]) -> None:
    print("\nPrevious forecast comparison")
    print("-" * 96)
    print(
        f"{'Horizon':<9}{'Predicted':>14}{'Actual':>14}{'Error':>11}"
        f"{'Direction':>13}{'Q10-Q90':>12}{'Forecast origin':>23}"
    )
    print("-" * 96)

    for hour in TARGET_HOURS:
        horizon = f"{hour}h"
        score = reliability.get(horizon)
        if not score:
            print(f"+{horizon:<8}{'not enough history yet':>34}")
            continue

        origin = datetime.fromisoformat(score["forecast_origin_at"]).strftime("%Y-%m-%d %H:%M")
        print(
            f"+{horizon:<8}"
            f"${score['predicted_price_usd']:>12,.2f}"
            f"${score['actual_price_usd']:>12,.2f}"
            f"{score['absolute_error_pct']:>10.3f}%"
            f"{'correct' if score['direction_correct'] else 'wrong':>13}"
            f"{'inside' if score['within_q10_q90'] else 'outside':>12}"
            f"{origin:>23}"
        )


def main() -> None:
    closes, close_timestamps = get_completed_hourly_candles()
    current_price = float(closes[-1])
    latest_close_at = datetime.fromtimestamp(close_timestamps[-1], tz=timezone.utc)

    print(f"Loaded {len(closes)} completed hourly candles")
    print(f"Latest BTC/USD close: ${current_price:,.2f} at {latest_close_at.isoformat()}")

    history = load_forecast_history()
    reliability = score_forecast_history(history, closes, close_timestamps)

    model = load_model()
    outputs = list(
        model.predict_batch(
            contexts=[closes],
            horizon=FORECAST_HOURS,
            return_quantiles=True,
            use_symmetric_averaging=False,
        )
    )

    result = outputs[0]
    forecast = result.forecast
    quantiles = result.quantiles

    predictions: dict[str, dict[str, float]] = {}
    for hour in TARGET_HOURS:
        idx = hour - 1
        price = float(forecast[idx])
        q10 = float(quantiles[idx, 0])
        q50 = float(quantiles[idx, 4])
        q90 = float(quantiles[idx, 8])
        change_pct = (price / current_price - 1.0) * 100.0

        predictions[f"{hour}h"] = {
            "price_usd": round(price, 2),
            "change_pct": round(change_pct, 4),
            "q10_usd": round(q10, 2),
            "q50_usd": round(q50, 2),
            "q90_usd": round(q90, 2),
        }

    output: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pair": "BTC/USD",
        "source": "Kraken hourly OHLC",
        "model": MODEL_ID,
        "latest_close_at": latest_close_at.isoformat(),
        "latest_close_usd": round(current_price, 2),
        "predictions": predictions,
        "forecast_reliability": reliability,
        # Preserve the old field for consumers that still expect the +2h score.
        "previous_forecast_reliability": reliability.get("2h"),
    }

    OUTPUT_PATH.write_text(json.dumps(output, indent=2) + "\n")
    save_forecast_history(history, output)

    tweet = build_tweet(output)
    TWEET_PATH.write_text(tweet + "\n")

    print("\nBTC/USD TimesFM 3 forecast")
    print("-" * 72)
    print(f"{'Horizon':<9}{'Forecast':>16}{'Change':>12}{'Q10':>16}{'Q90':>16}")
    print("-" * 72)

    for horizon, prediction in predictions.items():
        print(
            f"+{horizon:<8}"
            f"${prediction['price_usd']:>14,.2f}"
            f"{prediction['change_pct']:>11.3f}%"
            f"${prediction['q10_usd']:>14,.2f}"
            f"${prediction['q90_usd']:>14,.2f}"
        )

    print_reliability(reliability)

    print(f"\nSaved forecast to {OUTPUT_PATH}")
    print(f"Saved forecast history to {STATE_PATH} ({min(len(history) + 1, HISTORY_LIMIT)} snapshots max)")
    print(f"Saved X post to {TWEET_PATH}:\n\n{tweet}")


if __name__ == "__main__":
    main()
