#!/usr/bin/env python3
"""Forecast BTC/USD 2, 4, 8 and 16 hours ahead using TimesFM 3."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

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


def get_completed_hourly_closes() -> np.ndarray:
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

    return np.asarray(
        [float(candle[4]) for candle in completed[-CONTEXT_POINTS:]],
        dtype=np.float32,
    )


def load_model() -> TimesFM3Evaluator:
    print(f"Loading {MODEL_ID} on CPU...")
    config = ModelConfig(
        checkpoint_path=MODEL_ID,
        per_core_batch_size=1,
        device="cpu",
    )
    return TimesFM3Evaluator(config)


def main() -> None:
    closes = get_completed_hourly_closes()
    current_price = float(closes[-1])

    print(f"Loaded {len(closes)} completed hourly candles")
    print(f"Latest BTC/USD close: ${current_price:,.2f}")

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

    predictions = {}
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

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pair": "BTC/USD",
        "source": "Kraken hourly OHLC",
        "model": MODEL_ID,
        "latest_close_usd": round(current_price, 2),
        "predictions": predictions,
    }

    OUTPUT_PATH.write_text(json.dumps(output, indent=2) + "\n")

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

    print(f"\nSaved to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
