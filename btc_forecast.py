#!/usr/bin/env python3
"""Forecast BTC/USD with TimesFM 3 and score the previous +2h prediction."""

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

MODEL_ID = "google/timesfm-3.0-pytorch"
OUTPUT_PATH = Path("forecast.json")
TWEET_PATH = Path("tweet.txt")
PREVIOUS_FORECAST_PATH = Path(".state/previous_forecast.json")


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


def load_previous_forecast() -> dict[str, Any] | None:
    if not PREVIOUS_FORECAST_PATH.exists():
        return None

    try:
        return json.loads(PREVIOUS_FORECAST_PATH.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Ignoring unreadable previous forecast state: {exc}")
        return None


def score_previous_forecast(
    previous: dict[str, Any] | None,
    closes: np.ndarray,
    close_timestamps: list[int],
) -> dict[str, Any] | None:
    """Score the previous run's +2h forecast against the matching actual close."""
    if not previous:
        return None

    try:
        previous_close = float(previous["latest_close_usd"])
        previous_close_at = datetime.fromisoformat(previous["latest_close_at"])
        prediction = previous["predictions"]["2h"]
        predicted_price = float(prediction["price_usd"])
        q10 = float(prediction["q10_usd"])
        q90 = float(prediction["q90_usd"])
    except (KeyError, TypeError, ValueError) as exc:
        print(f"Previous forecast has an incompatible format: {exc}")
        return None

    if previous_close_at.tzinfo is None:
        previous_close_at = previous_close_at.replace(tzinfo=timezone.utc)

    target_at = previous_close_at.astimezone(timezone.utc) + timedelta(hours=2)
    target_timestamp = int(target_at.timestamp())
    actual_by_timestamp = dict(zip(close_timestamps, map(float, closes), strict=True))
    actual_price = actual_by_timestamp.get(target_timestamp)

    if actual_price is None:
        print(f"No completed candle found for previous +2h target {target_at.isoformat()}")
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

    direction_correct = direction(predicted_price - previous_close) == direction(
        actual_price - previous_close
    )
    within_interval = q10 <= actual_price <= q90

    return {
        "horizon": "2h",
        "target_at": target_at.isoformat(),
        "predicted_price_usd": round(predicted_price, 2),
        "actual_price_usd": round(actual_price, 2),
        "absolute_error_usd": round(absolute_error_usd, 2),
        "absolute_error_pct": round(absolute_error_pct, 4),
        "predicted_change_pct": round(predicted_change_pct, 4),
        "actual_change_pct": round(actual_change_pct, 4),
        "direction_correct": direction_correct,
        "within_q10_q90": within_interval,
    }


def format_price(value: float) -> str:
    return f"${value:,.0f}"


def build_tweet(output: dict[str, Any]) -> str:
    predictions = output["predictions"]
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

    reliability = output.get("previous_forecast_reliability")
    if reliability:
        direction_mark = "OK" if reliability["direction_correct"] else "MISS"
        range_mark = "in range" if reliability["within_q10_q90"] else "out of range"
        lines.append(
            f"Prev +2h: {reliability['absolute_error_pct']:.2f}% error | "
            f"direction {direction_mark} | {range_mark}"
        )
    else:
        lines.append("Prev +2h: no comparable prior forecast yet")

    lines.append("Experimental - not financial advice.")
    tweet = "\n".join(lines)

    # Keep a little buffer below X's 280-character limit.
    if len(tweet) > 270 and reliability:
        lines[-2] = (
            f"Prev +2h: {reliability['absolute_error_pct']:.2f}% err | "
            f"dir {'OK' if reliability['direction_correct'] else 'MISS'}"
        )
        tweet = "\n".join(lines)

    if len(tweet) > 280:
        raise RuntimeError(f"Generated X post is too long: {len(tweet)} characters")

    return tweet


def main() -> None:
    closes, close_timestamps = get_completed_hourly_candles()
    current_price = float(closes[-1])
    latest_close_at = datetime.fromtimestamp(close_timestamps[-1], tz=timezone.utc)

    print(f"Loaded {len(closes)} completed hourly candles")
    print(f"Latest BTC/USD close: ${current_price:,.2f} at {latest_close_at.isoformat()}")

    previous = load_previous_forecast()
    previous_reliability = score_previous_forecast(previous, closes, close_timestamps)

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
        "previous_forecast_reliability": previous_reliability,
    }

    OUTPUT_PATH.write_text(json.dumps(output, indent=2) + "\n")
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

    if previous_reliability:
        print(
            "\nPrevious +2h forecast: "
            f"{previous_reliability['absolute_error_pct']:.3f}% absolute error; "
            f"direction {'correct' if previous_reliability['direction_correct'] else 'wrong'}; "
            f"actual {'inside' if previous_reliability['within_q10_q90'] else 'outside'} Q10-Q90"
        )
    else:
        print("\nNo previous +2h forecast available to score yet.")

    print(f"\nSaved forecast to {OUTPUT_PATH}")
    print(f"Saved X post to {TWEET_PATH}:\n\n{tweet}")


if __name__ == "__main__":
    main()
