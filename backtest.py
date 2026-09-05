#!/usr/bin/env python3
"""Walk-forward backtest for the BTC forecast ensemble.

Historical candles come from Binance BTCUSDT because Kraken's OHLC endpoint only
exposes a limited recent window. BTCUSDT is used as a liquid USD proxy for
research/backtesting; production forecasts continue to use Kraken BTC/USD.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import requests

from forecast_engine import (
    MarketData,
    TARGET_HOURS,
    build_forecast,
    load_timesfm,
    static_model_weights,
)


BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
REPORT_PATH = Path("backtest_report.json")


def fetch_binance_history(days: int) -> MarketData:
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    rows: list[list[Any]] = []
    cursor = start_ms

    while cursor < end_ms:
        response = requests.get(
            BINANCE_KLINES,
            params={
                "symbol": "BTCUSDT",
                "interval": "1h",
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1000,
            },
            timeout=30,
        )
        response.raise_for_status()
        batch = response.json()
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1][0]) + 3600_000
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        time.sleep(0.05)

    if len(rows) < 550:
        raise RuntimeError(f"Not enough historical candles: {len(rows)}")

    timestamps = [int(row[0] / 1000) + 3600 for row in rows]
    return MarketData(
        timestamps=timestamps,
        opens=np.asarray([float(row[1]) for row in rows], dtype=np.float32),
        highs=np.asarray([float(row[2]) for row in rows], dtype=np.float32),
        lows=np.asarray([float(row[3]) for row in rows], dtype=np.float32),
        closes=np.asarray([float(row[4]) for row in rows], dtype=np.float32),
        volumes=np.asarray([float(row[5]) for row in rows], dtype=np.float32),
    )


def slice_market(data: MarketData, end_index: int, context: int = 513) -> MarketData:
    start = max(0, end_index - context + 1)
    sl = slice(start, end_index + 1)
    return MarketData(
        timestamps=data.timestamps[start : end_index + 1],
        opens=data.opens[sl],
        highs=data.highs[sl],
        lows=data.lows[sl],
        closes=data.closes[sl],
        volumes=data.volumes[sl],
    )


def direction(value: float, epsilon: float = 1e-9) -> int:
    return 1 if value > epsilon else -1 if value < -epsilon else 0


def score_one(current: float, predicted: float, actual: float) -> dict[str, Any]:
    error = predicted - actual
    return {
        "absolute_error_pct": abs(error) / actual * 100.0,
        "signed_error_pct": error / actual * 100.0,
        "direction_correct": direction(predicted - current) == direction(actual - current),
    }


def static_ensemble_price(forecast: dict[str, Any], horizon: str) -> float:
    current = float(forecast["latest_close_usd"])
    model_predictions = forecast["model_predictions"]
    weights = static_model_weights(list(model_predictions), forecast["regime"])
    log_change = 0.0
    for name, weight in weights.items():
        price = float(model_predictions[name][horizon]["price_usd"])
        log_change += weight * math.log(price / current)
    return current * math.exp(log_change)


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for horizon in ("2h", "4h", "8h", "16h"):
        per_model: dict[str, list[dict[str, Any]]] = {}
        ensemble_coverage: list[bool] = []
        regime_scores: dict[str, list[dict[str, Any]]] = {}

        for sample in samples:
            actual = sample["actuals"][horizon]
            current = sample["current_price"]
            ensemble = sample["forecast"]["predictions"][horizon]
            score = score_one(current, ensemble["price_usd"], actual)
            per_model.setdefault("adaptive_ensemble", []).append(score)
            ensemble_coverage.append(ensemble["q10_usd"] <= actual <= ensemble["q90_usd"])
            regime_scores.setdefault(sample["forecast"]["regime"], []).append(score)

            static_price = static_ensemble_price(sample["forecast"], horizon)
            per_model.setdefault("static_ensemble", []).append(
                score_one(current, static_price, actual)
            )

            for name, horizons in sample["forecast"]["model_predictions"].items():
                per_model.setdefault(name, []).append(
                    score_one(current, horizons[horizon]["price_usd"], actual)
                )

        models: dict[str, Any] = {}
        for name, scores in sorted(per_model.items()):
            models[name] = {
                "samples": len(scores),
                "mae_pct": round(float(np.mean([s["absolute_error_pct"] for s in scores])), 4),
                "mean_signed_error_pct": round(float(np.mean([s["signed_error_pct"] for s in scores])), 4),
                "direction_accuracy": round(float(np.mean([s["direction_correct"] for s in scores])), 4),
            }
            if name == "adaptive_ensemble":
                models[name]["q10_q90_coverage"] = round(float(np.mean(ensemble_coverage)), 4)

        by_regime = {
            regime: {
                "samples": len(scores),
                "mae_pct": round(float(np.mean([s["absolute_error_pct"] for s in scores])), 4),
                "direction_accuracy": round(float(np.mean([s["direction_correct"] for s in scores])), 4),
            }
            for regime, scores in sorted(regime_scores.items())
        }
        result[horizon] = {"models": models, "adaptive_ensemble_by_regime": by_regime}
    return result


def history_snapshot(forecast: dict[str, Any]) -> dict[str, Any]:
    return {
        "latest_close_at": forecast["latest_close_at"],
        "latest_close_usd": forecast["latest_close_usd"],
        "regime": forecast["regime"],
        "model_predictions": forecast["model_predictions"],
        "predictions": forecast["predictions"],
        "model_weights": forecast["model_weights"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--samples", type=int, default=60)
    args = parser.parse_args()

    data = fetch_binance_history(args.days)
    first = 513
    last = len(data.closes) - max(TARGET_HOURS) - 1
    if last <= first:
        raise RuntimeError("Historical window is too small for the requested backtest")

    candidate_indices = np.linspace(first, last, num=min(args.samples, last - first + 1), dtype=int)
    indices = sorted(set(map(int, candidate_indices)))
    model = load_timesfm()
    samples: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []

    for number, index in enumerate(indices, start=1):
        context = slice_market(data, index)
        forecast = build_forecast(model, context, history=history)
        actuals = {
            f"{hour}h": float(data.closes[index + hour])
            for hour in TARGET_HOURS
        }
        samples.append(
            {
                "origin_at": datetime.fromtimestamp(data.timestamps[index], tz=timezone.utc).isoformat(),
                "current_price": float(data.closes[index]),
                "actuals": actuals,
                "forecast": forecast,
            }
        )
        history.append(history_snapshot(forecast))
        history = history[-72:]
        modes = sorted({str(p["weighting_mode"]) for p in forecast["predictions"].values()})
        print(
            f"Backtest {number}/{len(indices)}: {samples[-1]['origin_at']} "
            f"({forecast['regime']}; {','.join(modes)})"
        )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_source": "Binance BTCUSDT 1h (historical proxy for BTC/USD)",
        "days_requested": args.days,
        "samples": len(samples),
        "summary": summarize(samples),
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n")

    print("\nBacktest summary")
    for horizon, info in report["summary"].items():
        adaptive = info["models"]["adaptive_ensemble"]
        static = info["models"]["static_ensemble"]
        persistence = info["models"].get("persistence", {})
        print(
            f"{horizon}: adaptive MAE {adaptive['mae_pct']:.3f}% / "
            f"static {static['mae_pct']:.3f}% / persistence "
            f"{persistence.get('mae_pct', float('nan')):.3f}%"
        )
    print(f"\nSaved {REPORT_PATH}")


if __name__ == "__main__":
    main()
