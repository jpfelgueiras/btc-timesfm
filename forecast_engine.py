"""Forecasting engine for BTC/USD.

TimesFM predicts log returns across several context windows. The engine combines
those forecasts with simple baselines, market/regime features, adaptive
performance-based weighting, model agreement, and empirical interval calibration
from recent forecast history.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
import requests
from timesfm3 import ModelConfig, TimesFM3Evaluator


KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
PAIR = "XBTUSD"
INTERVAL_MINUTES = 60
MODEL_ID = "google/timesfm-3.0-pytorch"
TARGET_HOURS = (2, 4, 8, 16)
FORECAST_HOURS = max(TARGET_HOURS)
CONTEXT_WINDOWS = (168, 336, 512)

ADAPTIVE_HISTORY_LIMIT = 72
ADAPTIVE_MIN_SAMPLES = 6
ADAPTIVE_FULL_SAMPLES = 24
ADAPTIVE_MAX_BLEND = 0.80
ADAPTIVE_MIN_WEIGHT = 0.03
ADAPTIVE_MAX_WEIGHT = 0.55
ADAPTIVE_MAE_LAMBDA = 2.5
ADAPTIVE_DIRECTION_REWARD = 0.25
PERSISTENCE_FALLBACK_BOOST = 0.12


@dataclass
class MarketData:
    timestamps: list[int]
    opens: np.ndarray
    highs: np.ndarray
    lows: np.ndarray
    closes: np.ndarray
    volumes: np.ndarray

    @property
    def returns(self) -> np.ndarray:
        return np.diff(np.log(self.closes.astype(np.float64))).astype(np.float32)


def fetch_kraken_hourly(limit: int = 512) -> MarketData:
    # N hourly returns require N+1 closes. Keep enough candles for the largest context.
    limit = max(limit, max(CONTEXT_WINDOWS) + 1)
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
    now = time.time()
    completed = [
        candle for candle in result[pair_key] if float(candle[0]) + INTERVAL_MINUTES * 60 <= now
    ]
    if len(completed) < 64:
        raise RuntimeError(f"Not enough completed candles: {len(completed)}")
    completed = completed[-limit:]

    def arr(index: int) -> np.ndarray:
        return np.asarray([float(c[index]) for c in completed], dtype=np.float32)

    return MarketData(
        timestamps=[int(float(c[0])) + INTERVAL_MINUTES * 60 for c in completed],
        opens=arr(1),
        highs=arr(2),
        lows=arr(3),
        closes=arr(4),
        volumes=arr(6),
    )


def load_timesfm() -> TimesFM3Evaluator:
    print(f"Loading {MODEL_ID} on CPU...")
    return TimesFM3Evaluator(
        ModelConfig(
            checkpoint_path=MODEL_ID,
            per_core_batch_size=1,
            device="cpu",
        )
    )


def _safe_std(values: np.ndarray) -> float:
    return float(np.std(values, ddof=1)) if len(values) > 1 else 0.0


def _rsi(closes: np.ndarray, period: int = 14) -> float:
    if len(closes) <= period:
        return 50.0
    delta = np.diff(closes.astype(np.float64))[-period:]
    gains = np.clip(delta, 0, None)
    losses = -np.clip(delta, None, 0)
    avg_gain = float(np.mean(gains))
    avg_loss = float(np.mean(losses))
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _pct_change(closes: np.ndarray, hours: int) -> float:
    if len(closes) <= hours:
        return 0.0
    return (float(closes[-1]) / float(closes[-1 - hours]) - 1.0) * 100.0


def market_features(data: MarketData) -> dict[str, Any]:
    returns = data.returns.astype(np.float64)
    ranges = (data.highs.astype(np.float64) - data.lows) / data.closes * 100.0
    log_volume = np.log1p(data.volumes.astype(np.float64))

    def vol(hours: int) -> float:
        return _safe_std(returns[-hours:]) * 100.0

    volume_window = log_volume[-168:]
    volume_std = _safe_std(volume_window)
    volume_z = (
        (float(log_volume[-1]) - float(np.mean(volume_window))) / volume_std
        if volume_std > 1e-12
        else 0.0
    )

    latest_dt = datetime.fromtimestamp(data.timestamps[-1], tz=timezone.utc)
    return {
        "close_usd": round(float(data.closes[-1]), 2),
        "volatility_6h_pct": round(vol(6), 4),
        "volatility_24h_pct": round(vol(24), 4),
        "volatility_7d_pct": round(vol(168), 4),
        "range_24h_avg_pct": round(float(np.mean(ranges[-24:])), 4),
        "volume_zscore_7d": round(volume_z, 4),
        "rsi_14": round(_rsi(data.closes), 2),
        "momentum_6h_pct": round(_pct_change(data.closes, 6), 4),
        "momentum_24h_pct": round(_pct_change(data.closes, 24), 4),
        "momentum_7d_pct": round(_pct_change(data.closes, 168), 4),
        "hour_utc": latest_dt.hour,
        "weekday_utc": latest_dt.weekday(),
        "hour_sin": round(math.sin(2 * math.pi * latest_dt.hour / 24), 4),
        "hour_cos": round(math.cos(2 * math.pi * latest_dt.hour / 24), 4),
        "weekday_sin": round(math.sin(2 * math.pi * latest_dt.weekday() / 7), 4),
        "weekday_cos": round(math.cos(2 * math.pi * latest_dt.weekday() / 7), 4),
    }


def detect_regime(features: dict[str, Any]) -> str:
    vol24 = float(features["volatility_24h_pct"])
    vol7d = max(float(features["volatility_7d_pct"]), 1e-6)
    mom24 = abs(float(features["momentum_24h_pct"]))
    rsi = float(features["rsi_14"])
    if vol24 > vol7d * 1.35:
        return "high_volatility"
    if mom24 > max(1.0, vol24 * math.sqrt(24) * 1.2) or rsi >= 70 or rsi <= 30:
        return "trending"
    return "range"


def _forecast_prices_from_return_path(
    current_price: float,
    return_path: np.ndarray,
) -> dict[str, float]:
    cumulative = np.cumsum(return_path.astype(np.float64))
    return {
        f"{hour}h": float(current_price * math.exp(float(cumulative[hour - 1])))
        for hour in TARGET_HOURS
    }


def timesfm_multi_context(
    model: TimesFM3Evaluator,
    data: MarketData,
) -> dict[str, dict[str, dict[str, float]]]:
    """Forecast log returns using several context windows."""
    returns = data.returns
    available = [window for window in CONTEXT_WINDOWS if len(returns) >= window]
    if not available:
        available = [len(returns)]

    outputs = list(
        model.predict_batch(
            contexts=[returns[-window:] for window in available],
            horizon=FORECAST_HOURS,
            return_quantiles=True,
            use_symmetric_averaging=False,
        )
    )
    current_price = float(data.closes[-1])
    forecasts: dict[str, dict[str, dict[str, float]]] = {}

    for window, result in zip(available, outputs, strict=True):
        point = np.asarray(result.forecast, dtype=np.float64)
        quantiles = np.asarray(result.quantiles, dtype=np.float64)
        point_prices = _forecast_prices_from_return_path(current_price, point)
        q10_prices = _forecast_prices_from_return_path(current_price, quantiles[:, 0])
        q50_prices = _forecast_prices_from_return_path(current_price, quantiles[:, 4])
        q90_prices = _forecast_prices_from_return_path(current_price, quantiles[:, 8])

        name = f"timesfm_{window}h"
        forecasts[name] = {}
        for hour in TARGET_HOURS:
            key = f"{hour}h"
            forecasts[name][key] = {
                "price_usd": point_prices[key],
                "q10_usd": q10_prices[key],
                "q50_usd": q50_prices[key],
                "q90_usd": q90_prices[key],
            }
    return forecasts


def baseline_forecasts(data: MarketData) -> dict[str, dict[str, dict[str, float]]]:
    returns = data.returns.astype(np.float64)
    current_price = float(data.closes[-1])
    recent_sigma = max(_safe_std(returns[-168:]), 1e-6)

    persistence_path = np.zeros(FORECAST_HOURS, dtype=np.float64)

    drift = float(np.mean(returns[-168:]))
    drift = float(np.clip(drift, -2.0 * recent_sigma, 2.0 * recent_sigma))
    drift_path = np.full(FORECAST_HOURS, drift, dtype=np.float64)

    x = returns[-169:-1] if len(returns) >= 170 else returns[:-1]
    y = returns[-168:] if len(returns) >= 170 else returns[1:]
    if len(x) >= 10 and float(np.var(x)) > 1e-12:
        phi = float(np.cov(x, y, ddof=0)[0, 1] / np.var(x))
        phi = float(np.clip(phi, -0.95, 0.95))
        intercept = float(np.mean(y) - phi * np.mean(x))
    else:
        phi, intercept = 0.0, drift

    ar_path = np.empty(FORECAST_HOURS, dtype=np.float64)
    last = float(returns[-1])
    for i in range(FORECAST_HOURS):
        last = intercept + phi * last
        last = float(np.clip(last, -4.0 * recent_sigma, 4.0 * recent_sigma))
        ar_path[i] = last

    models = {
        "persistence": persistence_path,
        "drift_7d": drift_path,
        "ar1": ar_path,
    }
    result: dict[str, dict[str, dict[str, float]]] = {}
    for name, path in models.items():
        prices = _forecast_prices_from_return_path(current_price, path)
        result[name] = {key: {"price_usd": price} for key, price in prices.items()}
    return result


def static_model_weights(model_names: list[str], regime: str) -> dict[str, float]:
    """Return the existing hand-tuned regime prior."""
    timesfm_names = [name for name in model_names if name.startswith("timesfm_")]
    if regime == "high_volatility":
        family = {"timesfm": 0.72, "persistence": 0.14, "drift_7d": 0.04, "ar1": 0.10}
    elif regime == "trending":
        family = {"timesfm": 0.65, "persistence": 0.08, "drift_7d": 0.17, "ar1": 0.10}
    else:
        family = {"timesfm": 0.57, "persistence": 0.24, "drift_7d": 0.06, "ar1": 0.13}

    context_rank = {"timesfm_168h": 0.25, "timesfm_336h": 0.35, "timesfm_512h": 0.40}
    raw_context = np.asarray([context_rank.get(name, 1.0) for name in timesfm_names], dtype=float)
    if len(raw_context):
        raw_context /= raw_context.sum()

    weights = {
        name: family["timesfm"] * float(weight)
        for name, weight in zip(timesfm_names, raw_context, strict=True)
    }
    for name in ("persistence", "drift_7d", "ar1"):
        if name in model_names:
            weights[name] = family[name]
    total = sum(weights.values())
    return {name: weight / total for name, weight in weights.items()}


model_weights = static_model_weights


def _direction(value: float, epsilon: float = 1e-12) -> int:
    return 1 if value > epsilon else -1 if value < -epsilon else 0


def _bounded_normalize(
    weights: dict[str, float],
    floor: float = ADAPTIVE_MIN_WEIGHT,
    cap: float = ADAPTIVE_MAX_WEIGHT,
) -> dict[str, float]:
    """Normalize positive weights while strictly respecting floors and caps.

    The result is the clipped proportional distribution ``scale * raw_weight``.
    A monotonic bisection solves for the scale that makes the clipped weights sum
    to one. This avoids renormalizing after clipping, which can re-violate a cap.
    """
    names = list(weights)
    if not names:
        return {}
    if floor * len(names) > 1.0 or cap * len(names) < 1.0:
        raise ValueError("Weight floor/cap cannot produce a normalized distribution")

    raw = {name: max(float(weights[name]), 1e-12) for name in names}

    def clipped_total(scale: float) -> float:
        return sum(min(cap, max(floor, scale * raw[name])) for name in names)

    low = 0.0
    high = 1.0
    while clipped_total(high) < 1.0:
        high *= 2.0

    for _ in range(100):
        mid = (low + high) / 2.0
        if clipped_total(mid) < 1.0:
            low = mid
        else:
            high = mid

    result = {name: min(cap, max(floor, high * raw[name])) for name in names}

    # Bisection is already within machine precision. Correct any final rounding
    # residue using a weight that still has room without violating its bounds.
    residue = 1.0 - sum(result.values())
    if abs(residue) > 1e-12:
        candidates = [
            name
            for name, value in result.items()
            if (residue > 0 and value < cap) or (residue < 0 and value > floor)
        ]
        if candidates:
            name = candidates[0]
            result[name] = min(cap, max(floor, result[name] + residue))

    return result


def _score_history_for_model(
    history: list[dict[str, Any]],
    actual_by_timestamp: dict[int, float],
    model_name: str,
    hour: int,
    regime: str | None,
) -> list[dict[str, float | bool]]:
    key = f"{hour}h"
    scores: list[dict[str, float | bool]] = []

    for snapshot in history[-ADAPTIVE_HISTORY_LIMIT:]:
        if regime is not None and snapshot.get("regime") != regime:
            continue
        try:
            origin = datetime.fromisoformat(str(snapshot["latest_close_at"]))
            if origin.tzinfo is None:
                origin = origin.replace(tzinfo=timezone.utc)
            origin = origin.astimezone(timezone.utc)
            previous_close = float(snapshot["latest_close_usd"])
            predicted = float(snapshot["model_predictions"][model_name][key]["price_usd"])
        except (KeyError, TypeError, ValueError):
            continue

        target = int(origin.timestamp()) + hour * 3600
        actual = actual_by_timestamp.get(target)
        if actual is None or actual <= 0:
            continue

        error = predicted - actual
        scores.append(
            {
                "absolute_error_pct": abs(error) / actual * 100.0,
                "signed_error_pct": error / actual * 100.0,
                "direction_correct": _direction(predicted - previous_close)
                == _direction(actual - previous_close),
            }
        )

    return scores


def _performance_metrics(scores: list[dict[str, float | bool]]) -> dict[str, float | int]:
    return {
        "samples": len(scores),
        "mae_pct": float(np.mean([float(s["absolute_error_pct"]) for s in scores])),
        "mean_signed_error_pct": float(np.mean([float(s["signed_error_pct"]) for s in scores])),
        "direction_accuracy": float(np.mean([bool(s["direction_correct"]) for s in scores])),
    }


def adaptive_model_weights(
    model_names: list[str],
    regime: str,
    hour: int,
    history: list[dict[str, Any]],
    actual_by_timestamp: dict[int, float],
    enabled: bool = True,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Blend static priors with recent out-of-sample model performance."""
    prior = static_model_weights(model_names, regime)

    regime_scores = {
        name: _score_history_for_model(history, actual_by_timestamp, name, hour, regime)
        for name in model_names
    }
    all_scores = {
        name: _score_history_for_model(history, actual_by_timestamp, name, hour, None)
        for name in model_names
    }

    min_regime_samples = min((len(scores) for scores in regime_scores.values()), default=0)
    min_all_samples = min((len(scores) for scores in all_scores.values()), default=0)

    if not enabled:
        source = "disabled"
        selected = all_scores
    elif min_regime_samples >= ADAPTIVE_MIN_SAMPLES:
        source = "regime"
        selected = regime_scores
    elif min_all_samples >= ADAPTIVE_MIN_SAMPLES:
        source = "all_regimes"
        selected = all_scores
    else:
        source = "insufficient_history"
        selected = all_scores

    metrics = {
        name: _performance_metrics(scores)
        if scores
        else {
            "samples": 0,
            "mae_pct": None,
            "mean_signed_error_pct": None,
            "direction_accuracy": None,
        }
        for name, scores in selected.items()
    }

    if not enabled or source == "insufficient_history":
        diagnostics = {
            "mode": "static_prior",
            "source": source,
            "horizon": f"{hour}h",
            "regime": regime,
            "blend_factor": 0.0,
            "sample_count": min_all_samples if source != "regime" else min_regime_samples,
            "persistence_mae_pct": (
                metrics.get("persistence", {}).get("mae_pct") if "persistence" in metrics else None
            ),
            "models": {
                name: {
                    **metric,
                    "prior_weight": round(prior[name], 6),
                    "raw_score": None,
                    "final_weight": round(prior[name], 6),
                    "edge_vs_persistence_mae_pct": None,
                }
                for name, metric in metrics.items()
            },
        }
        return prior, diagnostics

    persistence_mae = float(metrics["persistence"]["mae_pct"]) if "persistence" in metrics else None
    raw_scores: dict[str, float] = {}
    for name, metric in metrics.items():
        mae = float(metric["mae_pct"])
        direction_accuracy = float(metric["direction_accuracy"])
        bias = abs(float(metric["mean_signed_error_pct"]))

        score = math.exp(-ADAPTIVE_MAE_LAMBDA * mae)
        score *= 1.0 + ADAPTIVE_DIRECTION_REWARD * (direction_accuracy - 0.5) * 2.0
        score *= math.exp(-0.35 * bias)

        if persistence_mae is not None and name != "persistence":
            edge = persistence_mae - mae
            if edge < 0:
                score *= math.exp(1.5 * edge)

        raw_scores[name] = max(score, 1e-9)

    raw_total = sum(raw_scores.values())
    adaptive = {name: score / raw_total for name, score in raw_scores.items()}

    sample_count = min(int(metric["samples"]) for metric in metrics.values())
    progress = min(
        1.0,
        max(
            0.0,
            (sample_count - ADAPTIVE_MIN_SAMPLES)
            / max(1, ADAPTIVE_FULL_SAMPLES - ADAPTIVE_MIN_SAMPLES),
        ),
    )
    blend = 0.25 + (ADAPTIVE_MAX_BLEND - 0.25) * progress

    blended = {name: (1.0 - blend) * prior[name] + blend * adaptive[name] for name in model_names}

    complex_maes = [
        float(metrics[name]["mae_pct"])
        for name in model_names
        if name != "persistence" and metrics[name]["mae_pct"] is not None
    ]
    persistence_fallback = False
    if (
        persistence_mae is not None
        and complex_maes
        and float(np.mean(complex_maes)) >= persistence_mae
        and "persistence" in blended
    ):
        blended["persistence"] += PERSISTENCE_FALLBACK_BOOST
        persistence_fallback = True

    final = _bounded_normalize(blended)

    diagnostics = {
        "mode": "adaptive",
        "source": source,
        "horizon": f"{hour}h",
        "regime": regime,
        "blend_factor": round(blend, 4),
        "sample_count": sample_count,
        "persistence_fallback": persistence_fallback,
        "persistence_mae_pct": round(persistence_mae, 6) if persistence_mae is not None else None,
        "models": {},
    }
    for name, metric in metrics.items():
        mae = float(metric["mae_pct"])
        diagnostics["models"][name] = {
            **{
                key: round(value, 6) if isinstance(value, float) else value
                for key, value in metric.items()
            },
            "prior_weight": round(prior[name], 6),
            "raw_score": round(raw_scores[name], 8),
            "adaptive_weight": round(adaptive[name], 6),
            "final_weight": round(final[name], 6),
            "edge_vs_persistence_mae_pct": (
                round(persistence_mae - mae, 6) if persistence_mae is not None else None
            ),
        }

    return final, diagnostics


def empirical_calibration_multiplier(
    history: list[dict[str, Any]],
    actual_by_timestamp: dict[int, float],
    hour: int,
    target_coverage: float = 0.80,
) -> tuple[float, int, float | None]:
    observations: list[bool] = []
    for snapshot in history[-48:]:
        try:
            origin = datetime.fromisoformat(snapshot["latest_close_at"])
            if origin.tzinfo is None:
                origin = origin.replace(tzinfo=timezone.utc)
            target = int(origin.astimezone(timezone.utc).timestamp()) + hour * 3600
            actual = actual_by_timestamp.get(target)
            pred = snapshot["predictions"][f"{hour}h"]
            if actual is None:
                continue
            observations.append(float(pred["q10_usd"]) <= actual <= float(pred["q90_usd"]))
        except (KeyError, TypeError, ValueError):
            continue

    if len(observations) < 10:
        return 1.0, len(observations), None
    coverage = sum(observations) / len(observations)
    multiplier = math.sqrt(target_coverage / max(coverage, 0.10))
    return float(np.clip(multiplier, 0.75, 1.75)), len(observations), coverage


def ensemble_forecast(
    data: MarketData,
    model_predictions: dict[str, dict[str, dict[str, float]]],
    regime: str,
    calibration: dict[str, tuple[float, int, float | None]],
    history: list[dict[str, Any]],
    actual_by_timestamp: dict[int, float],
    adaptive_weights_enabled: bool = True,
) -> tuple[
    dict[str, dict[str, float | str]],
    dict[str, dict[str, float]],
    dict[str, dict[str, Any]],
]:
    names = list(model_predictions)
    current_price = float(data.closes[-1])
    predictions: dict[str, dict[str, float | str]] = {}
    weights_by_horizon: dict[str, dict[str, float]] = {}
    weighting_diagnostics: dict[str, dict[str, Any]] = {}

    for hour in TARGET_HOURS:
        key = f"{hour}h"
        weights, diagnostics = adaptive_model_weights(
            names,
            regime,
            hour,
            history,
            actual_by_timestamp,
            enabled=adaptive_weights_enabled,
        )
        weights_by_horizon[key] = weights
        weighting_diagnostics[key] = diagnostics

        log_changes: list[float] = []
        model_prices: list[float] = []
        model_ws: list[float] = []
        timesfm_half_widths: list[float] = []

        for name in names:
            item = model_predictions[name][key]
            price = float(item["price_usd"])
            weight = weights.get(name, 0.0)
            if weight <= 0:
                continue
            log_changes.append(math.log(price / current_price))
            model_prices.append(price)
            model_ws.append(weight)
            if "q10_usd" in item and "q90_usd" in item:
                timesfm_half_widths.append(
                    max(0.0, (float(item["q90_usd"]) - float(item["q10_usd"])) / 2.0)
                )

        normalized = np.asarray(model_ws, dtype=float)
        normalized /= normalized.sum()
        ensemble_log_change = float(np.dot(normalized, np.asarray(log_changes)))
        price = current_price * math.exp(ensemble_log_change)
        dispersion = float(math.sqrt(np.dot(normalized, (np.asarray(model_prices) - price) ** 2)))
        tf_width = float(np.mean(timesfm_half_widths)) if timesfm_half_widths else 0.0
        base_half_width = max(tf_width, dispersion * 1.5, current_price * 0.0005)

        multiplier, sample_count, empirical_coverage = calibration[key]
        half_width = base_half_width * multiplier
        q10 = max(0.01, price - half_width)
        q90 = price + half_width

        moves = [1 if p > current_price else -1 if p < current_price else 0 for p in model_prices]
        agreement = max(moves.count(1), moves.count(-1), moves.count(0)) / len(moves)
        predictions[key] = {
            "price_usd": round(price, 2),
            "change_pct": round((price / current_price - 1.0) * 100.0, 4),
            "q10_usd": round(q10, 2),
            "q50_usd": round(price, 2),
            "q90_usd": round(q90, 2),
            "model_agreement": round(agreement, 4),
            "weighting_mode": str(diagnostics["mode"]),
            "weighting_samples": int(diagnostics["sample_count"]),
            "interval_calibration_multiplier": round(multiplier, 4),
            "calibration_samples": sample_count,
            "empirical_q10_q90_coverage": (
                round(empirical_coverage, 4) if empirical_coverage is not None else None
            ),
        }

    return predictions, weights_by_horizon, weighting_diagnostics


def build_forecast(
    model: TimesFM3Evaluator,
    data: MarketData,
    history: list[dict[str, Any]] | None = None,
    adaptive_weights_enabled: bool = True,
) -> dict[str, Any]:
    history = history or []
    features = market_features(data)
    regime = detect_regime(features)
    models = {**timesfm_multi_context(model, data), **baseline_forecasts(data)}

    actuals = dict(zip(data.timestamps, map(float, data.closes), strict=True))
    calibration = {
        f"{hour}h": empirical_calibration_multiplier(history, actuals, hour)
        for hour in TARGET_HOURS
    }
    predictions, weights, weighting_diagnostics = ensemble_forecast(
        data,
        models,
        regime,
        calibration,
        history,
        actuals,
        adaptive_weights_enabled=adaptive_weights_enabled,
    )

    return {
        "model": MODEL_ID,
        "forecast_method": "log-return multi-context adaptive ensemble",
        "latest_close_at": datetime.fromtimestamp(data.timestamps[-1], tz=timezone.utc).isoformat(),
        "latest_close_usd": round(float(data.closes[-1]), 2),
        "market_features": features,
        "regime": regime,
        "model_weights": {
            horizon: {name: round(weight, 4) for name, weight in horizon_weights.items()}
            for horizon, horizon_weights in weights.items()
        },
        "weighting_diagnostics": weighting_diagnostics,
        "model_predictions": {
            model_name: {
                horizon: {
                    key: round(value, 4) if isinstance(value, float) else value
                    for key, value in values.items()
                }
                for horizon, values in horizons.items()
            }
            for model_name, horizons in models.items()
        },
        "predictions": predictions,
    }
