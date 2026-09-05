#!/usr/bin/env python3
"""Forecast BTC/USD and evaluate matured multi-model predictions."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

import forecast_engine
from adaptive_weighting import adaptive_model_weights, attach_persisted_outcomes
from forecast_engine import TARGET_HOURS, build_forecast, fetch_kraken_hourly, load_timesfm
from history_store import DEFAULT_DB_PATH, ForecastHistoryStore


OUTPUT_PATH = Path("forecast.json")
TWEET_PATH = Path("tweet.txt")
STATE_PATH = Path(".state/previous_forecast.json")
HISTORY_DB_PATH = DEFAULT_DB_PATH
HISTORY_LIMIT = 72

# build_forecast resolves this function from forecast_engine's module globals.
# Install the issue #6 policy once so production uses the durable-history-aware
# weighting implementation while keeping the engine API stable.
forecast_engine.adaptive_model_weights = adaptive_model_weights


def load_forecast_history() -> list[dict[str, Any]]:
    if not STATE_PATH.exists():
        return []
    try:
        state = json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Ignoring unreadable forecast state: {exc}")
        return []

    if isinstance(state, dict) and isinstance(state.get("forecasts"), list):
        return [item for item in state["forecasts"] if isinstance(item, dict)]
    if isinstance(state, dict) and "predictions" in state:
        return [state]
    return []


def _direction(value: float, epsilon: float = 1e-9) -> int:
    return 1 if value > epsilon else -1 if value < -epsilon else 0


def score_price(
    previous_close: float,
    predicted_price: float,
    actual_price: float,
    q10: float | None = None,
    q90: float | None = None,
) -> dict[str, Any]:
    error = predicted_price - actual_price
    absolute_error_usd = abs(error)
    absolute_error_pct = absolute_error_usd / actual_price * 100.0
    signed_error_pct = error / actual_price * 100.0
    predicted_change_pct = (predicted_price / previous_close - 1.0) * 100.0
    actual_change_pct = (actual_price / previous_close - 1.0) * 100.0
    return {
        "predicted_price_usd": round(predicted_price, 2),
        "actual_price_usd": round(actual_price, 2),
        "absolute_error_usd": round(absolute_error_usd, 2),
        "absolute_error_pct": round(absolute_error_pct, 4),
        "signed_error_pct": round(signed_error_pct, 4),
        "predicted_change_pct": round(predicted_change_pct, 4),
        "actual_change_pct": round(actual_change_pct, 4),
        "direction_correct": _direction(predicted_price - previous_close)
        == _direction(actual_price - previous_close),
        "within_q10_q90": (
            q10 <= actual_price <= q90 if q10 is not None and q90 is not None else None
        ),
    }


def score_snapshot(
    snapshot: dict[str, Any],
    hour: int,
    actual_by_timestamp: dict[int, float],
) -> dict[str, Any] | None:
    try:
        previous_close = float(snapshot["latest_close_usd"])
        origin = datetime.fromisoformat(snapshot["latest_close_at"])
        ensemble = snapshot["predictions"][f"{hour}h"]
    except (KeyError, TypeError, ValueError):
        return None

    if origin.tzinfo is None:
        origin = origin.replace(tzinfo=timezone.utc)
    origin = origin.astimezone(timezone.utc)
    target_at = origin + timedelta(hours=hour)
    actual = actual_by_timestamp.get(int(target_at.timestamp()))
    if actual is None:
        return None

    score = score_price(
        previous_close,
        float(ensemble["price_usd"]),
        actual,
        float(ensemble["q10_usd"]) if "q10_usd" in ensemble else None,
        float(ensemble["q90_usd"]) if "q90_usd" in ensemble else None,
    )
    score.update(
        {
            "horizon": f"{hour}h",
            "forecast_origin_at": origin.isoformat(),
            "target_at": target_at.isoformat(),
            "regime": snapshot.get("regime"),
        }
    )

    model_scores: dict[str, Any] = {}
    for model_name, horizons in snapshot.get("model_predictions", {}).items():
        try:
            item = horizons[f"{hour}h"]
            model_scores[model_name] = score_price(
                previous_close,
                float(item["price_usd"]),
                actual,
                float(item["q10_usd"]) if "q10_usd" in item else None,
                float(item["q90_usd"]) if "q90_usd" in item else None,
            )
        except (KeyError, TypeError, ValueError):
            continue
    score["models"] = model_scores
    return score


def score_forecast_history(
    history: list[dict[str, Any]],
    closes: np.ndarray,
    timestamps: list[int],
) -> dict[str, dict[str, Any]]:
    actuals = dict(zip(timestamps, map(float, closes), strict=True))
    reliability: dict[str, dict[str, Any]] = {}
    for hour in TARGET_HOURS:
        for snapshot in reversed(history):
            score = score_snapshot(snapshot, hour, actuals)
            if score is not None:
                reliability[f"{hour}h"] = score
                break
    return reliability


def performance_summary(
    history: list[dict[str, Any]],
    closes: np.ndarray,
    timestamps: list[int],
) -> dict[str, Any]:
    actuals = dict(zip(timestamps, map(float, closes), strict=True))
    result: dict[str, Any] = {}

    for hour in TARGET_HOURS:
        scores = [
            score
            for snapshot in history
            if (score := score_snapshot(snapshot, hour, actuals)) is not None
        ]
        if not scores:
            continue

        mae = float(np.mean([s["absolute_error_pct"] for s in scores]))
        bias = float(np.mean([s["signed_error_pct"] for s in scores]))
        direction = float(np.mean([s["direction_correct"] for s in scores]))
        covered = [s["within_q10_q90"] for s in scores if s["within_q10_q90"] is not None]
        coverage = float(np.mean(covered)) if covered else None

        model_names = sorted({name for score in scores for name in score.get("models", {})})
        models: dict[str, Any] = {}
        for name in model_names:
            model_scores = [s["models"][name] for s in scores if name in s.get("models", {})]
            models[name] = {
                "samples": len(model_scores),
                "mae_pct": round(
                    float(np.mean([s["absolute_error_pct"] for s in model_scores])), 4
                ),
                "direction_accuracy": round(
                    float(np.mean([s["direction_correct"] for s in model_scores])), 4
                ),
            }

        result[f"{hour}h"] = {
            "samples": len(scores),
            "mae_pct": round(mae, 4),
            "mean_signed_error_pct": round(bias, 4),
            "direction_accuracy": round(direction, 4),
            "q10_q90_coverage": round(coverage, 4) if coverage is not None else None,
            "models": models,
        }
    return result


def save_forecast_history(history: list[dict[str, Any]], output: dict[str, Any]) -> None:
    """Keep a small rolling cache for the scheduler; durable data lives in SQLite."""
    snapshot = {
        "generated_at": output["generated_at"],
        "latest_close_at": output["latest_close_at"],
        "latest_close_usd": output["latest_close_usd"],
        "source": output.get("source"),
        "pair": output.get("pair"),
        "regime": output["regime"],
        "market_features": output["market_features"],
        "model_weights": output["model_weights"],
        "model_predictions": output["model_predictions"],
        "predictions": output["predictions"],
    }
    deduplicated = [
        item for item in history if item.get("latest_close_at") != snapshot["latest_close_at"]
    ]
    deduplicated.append(snapshot)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps({"version": 3, "forecasts": deduplicated[-HISTORY_LIMIT:]}, indent=2) + "\n"
    )


def format_price(value: float) -> str:
    return f"${value:,.0f}"


def build_tweet(output: dict[str, Any]) -> str:
    predictions = output["predictions"]
    reliability = output.get("forecast_reliability", {})
    lines = [
        "BTC/USD ensemble forecast",
        f"Now {format_price(output['latest_close_usd'])} | {output['regime'].replace('_', ' ')}",
        " | ".join(
            f"{h} {format_price(predictions[h]['price_usd'])} ({predictions[h]['change_pct']:+.2f}%)"
            for h in ("2h", "4h")
        ),
        " | ".join(
            f"{h} {format_price(predictions[h]['price_usd'])} ({predictions[h]['change_pct']:+.2f}%)"
            for h in ("8h", "16h")
        ),
    ]
    errors = []
    for horizon in ("2h", "4h", "8h", "16h"):
        score = reliability.get(horizon)
        errors.append(f"{horizon} {score['absolute_error_pct']:.2f}%" if score else f"{horizon} --")
    lines.append("Prev err: " + " | ".join(errors))
    lines.append("Experimental - not financial advice.")
    tweet = "\n".join(lines)
    if len(tweet) > 280:
        lines[1] = f"Now {format_price(output['latest_close_usd'])}"
        tweet = "\n".join(lines)
    if len(tweet) > 280:
        raise RuntimeError(f"Generated X post is too long: {len(tweet)} characters")
    return tweet


def print_reliability(reliability: dict[str, dict[str, Any]]) -> None:
    print("\nMatured ensemble comparison")
    print("-" * 82)
    print(
        f"{'Horizon':<9}{'Predicted':>14}{'Actual':>14}{'MAE':>10}{'Bias':>10}{'Dir':>9}{'Band':>10}"
    )
    print("-" * 82)
    for hour in TARGET_HOURS:
        key = f"{hour}h"
        score = reliability.get(key)
        if not score:
            print(f"+{key:<8}{'not enough history yet':>34}")
            continue
        band = score["within_q10_q90"]
        print(
            f"+{key:<8}"
            f"${score['predicted_price_usd']:>12,.2f}"
            f"${score['actual_price_usd']:>12,.2f}"
            f"{score['absolute_error_pct']:>9.3f}%"
            f"{score['signed_error_pct']:>9.3f}%"
            f"{'OK' if score['direction_correct'] else 'MISS':>9}"
            f"{('in' if band else 'out') if band is not None else '--':>10}"
        )


def main() -> None:
    data = fetch_kraken_hourly(512)
    print(f"Loaded {len(data.closes)} completed hourly candles")
    print(
        f"Latest BTC/USD close: ${float(data.closes[-1]):,.2f} at "
        f"{datetime.fromtimestamp(data.timestamps[-1], tz=timezone.utc).isoformat()}"
    )

    actuals = dict(zip(data.timestamps, map(float, data.closes), strict=True))
    rolling_history = load_forecast_history()

    # Bootstrap/migrate the durable database from any rolling cache that predates
    # issue #5, then use the durable copy as the source of truth for weighting.
    store = ForecastHistoryStore(HISTORY_DB_PATH)
    migration = store.ingest_snapshots(rolling_history, actuals)
    durable_history = store.load_snapshots()
    if durable_history:
        history = attach_persisted_outcomes(durable_history, store.export_rows())
    else:
        history = rolling_history

    reliability = score_forecast_history(history, data.closes, data.timestamps)
    summary = store.performance_summary()
    if not summary:
        # Local/first-run fallback before any matured durable records exist.
        summary = performance_summary(history, data.closes, data.timestamps)

    model = load_timesfm()
    engine_output = build_forecast(model, data, history)
    output: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pair": "BTC/USD",
        "source": "Kraken hourly OHLC",
        **engine_output,
        "forecast_reliability": reliability,
        "performance_summary": summary,
        "previous_forecast_reliability": reliability.get("2h"),
    }

    # The scheduler still gets a tiny fast cache, while the SQLite store keeps
    # every logical forecast indefinitely and matures any exact target candles.
    save_forecast_history(rolling_history, output)
    persisted = store.ingest_snapshot(output, actuals)
    store.verify()
    output["history_store"] = {
        **store.stats(),
        "rolling_cache_migration": migration,
        "latest_ingest": persisted,
    }

    OUTPUT_PATH.write_text(json.dumps(output, indent=2) + "\n")
    tweet = build_tweet(output)
    TWEET_PATH.write_text(tweet + "\n")

    print(f"\nRegime: {output['regime']}")
    print(f"Model weights: {output['model_weights']}")
    print(f"Durable history: {output['history_store']}")
    print("\nBTC/USD ensemble forecast")
    print("-" * 78)
    print(f"{'Horizon':<9}{'Forecast':>16}{'Change':>12}{'Q10':>16}{'Q90':>16}{'Agree':>9}")
    print("-" * 78)
    for horizon, prediction in output["predictions"].items():
        print(
            f"+{horizon:<8}"
            f"${prediction['price_usd']:>14,.2f}"
            f"{prediction['change_pct']:>11.3f}%"
            f"${prediction['q10_usd']:>14,.2f}"
            f"${prediction['q90_usd']:>14,.2f}"
            f"{prediction['model_agreement'] * 100:>8.0f}%"
        )
    print_reliability(reliability)
    print(f"\nSaved forecast to {OUTPUT_PATH}")
    print(f"Saved X post to {TWEET_PATH}:\n\n{tweet}")


if __name__ == "__main__":
    main()
