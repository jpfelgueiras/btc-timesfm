"""Lightweight non-TimesFM forecasting model for BTC hourly returns.

The model is deliberately simple and CPU-friendly: a direct ridge regression is
fit independently for each production horizon using engineered, past-only
features. Training labels are only created when their target close already
exists inside the input window, so the same implementation is safe in both
production and chronological walk-forward evaluation.
"""

from __future__ import annotations

import math
import os
import sys
from typing import Any, Callable

import numpy as np

MODEL_NAME = "ridge_features"
TARGET_HOURS = (2, 4, 8, 16)
MAX_LAG = 72
DEFAULT_MIN_TRAIN_SAMPLES = int(os.getenv("BTC_RIDGE_MIN_TRAIN_SAMPLES", "96"))
DEFAULT_RIDGE_ALPHA = float(os.getenv("BTC_RIDGE_ALPHA", "8.0"))
DEFAULT_INITIAL_PRIOR_WEIGHT = float(os.getenv("BTC_RIDGE_INITIAL_PRIOR_WEIGHT", "0.08"))
MIN_INITIAL_PRIOR_WEIGHT = 0.03
MAX_INITIAL_PRIOR_WEIGHT = 0.20
PRODUCTION_FLAG = "BTC_ENABLE_DIVERSIFIED_MODEL"

StaticWeights = Callable[[list[str], str], dict[str, float]]


def production_enabled() -> bool:
    """Return whether the research model has been explicitly approved for production."""
    return os.getenv(PRODUCTION_FLAG, "false").strip().lower() in {"1", "true", "yes", "on"}


def diversified_static_model_weights(
    base_function: StaticWeights,
    model_names: list[str],
    regime: str,
    *,
    initial_weight: float = DEFAULT_INITIAL_PRIOR_WEIGHT,
) -> dict[str, float]:
    """Reserve a conservative static prior for the diversified model.

    Adaptive weighting falls back to the static prior until every active model
    has enough matured history. Without this reservation, adding the ridge model
    would leave it absent from the prior and the fallback path would raise a
    KeyError. Existing relative priors are preserved and scaled into the
    remaining mass.
    """
    if MODEL_NAME not in model_names:
        return base_function(model_names, regime)

    base_names = [name for name in model_names if name != MODEL_NAME]
    if not base_names:
        return {MODEL_NAME: 1.0}

    base = dict(base_function(base_names, regime))
    if not base:
        raise ValueError("base static weighting returned no weights")

    ridge_weight = float(
        np.clip(initial_weight, MIN_INITIAL_PRIOR_WEIGHT, MAX_INITIAL_PRIOR_WEIGHT)
    )
    base_total = sum(max(0.0, float(weight)) for weight in base.values())
    if base_total <= 0:
        raise ValueError("base static weighting must contain positive weight")

    remaining = 1.0 - ridge_weight
    weights = {
        name: remaining * max(0.0, float(weight)) / base_total for name, weight in base.items()
    }
    weights[MODEL_NAME] = ridge_weight
    return weights


def install_adaptive_prior() -> None:
    """Install the ridge-aware static prior when adaptive weighting is already loaded."""
    adaptive_weighting = sys.modules.get("adaptive_weighting")
    if adaptive_weighting is None:
        return
    if bool(getattr(adaptive_weighting, "_ridge_prior_installed", False)):
        return
    base_function = getattr(adaptive_weighting, "static_model_weights", None)
    if base_function is None:
        return

    def ridge_aware_weights(model_names: list[str], regime: str) -> dict[str, float]:
        return diversified_static_model_weights(base_function, model_names, regime)

    setattr(adaptive_weighting, "static_model_weights", ridge_aware_weights)
    setattr(adaptive_weighting, "_ridge_prior_installed", True)


def _safe_std(values: np.ndarray) -> float:
    value = float(np.std(values))
    return value if math.isfinite(value) and value > 1e-9 else 1.0


def _rsi_from_returns(returns: np.ndarray, period: int = 14) -> float:
    window = returns[-period:]
    gains = np.clip(window, 0.0, None)
    losses = np.clip(-window, 0.0, None)
    avg_gain = float(np.mean(gains))
    avg_loss = float(np.mean(losses))
    if avg_loss <= 1e-12:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _feature_at(data: Any, index: int) -> np.ndarray:
    closes = np.asarray(data.closes, dtype=float)
    highs = np.asarray(data.highs, dtype=float)
    lows = np.asarray(data.lows, dtype=float)
    volumes = np.asarray(data.volumes, dtype=float)
    if index < MAX_LAG or index >= len(closes):
        raise ValueError(f"feature index must be in [{MAX_LAG}, {len(closes) - 1}]")

    log_prices = np.log(closes[: index + 1])
    returns = np.diff(log_prices)
    last_return = returns[-1]
    lag_returns = [returns[-lag] for lag in (1, 2, 4, 8, 24)]
    momentum = [log_prices[-1] - log_prices[-1 - lag] for lag in (6, 24, 72)]
    mean_returns = [float(np.mean(returns[-window:])) for window in (6, 24, 72)]
    volatility = [float(np.std(returns[-window:])) for window in (6, 24, 72)]

    close = closes[index]
    range_fraction = float((highs[index] - lows[index]) / close)
    volume_window = volumes[index - 23 : index + 1]
    volume_z = float((volumes[index] - np.mean(volume_window)) / _safe_std(volume_window))
    rsi_scaled = (_rsi_from_returns(returns) - 50.0) / 50.0

    timestamp = int(data.timestamps[index])
    hour = (timestamp // 3600) % 24
    weekday = (timestamp // 86400 + 4) % 7
    hour_angle = 2.0 * math.pi * hour / 24.0
    weekday_angle = 2.0 * math.pi * weekday / 7.0

    features = np.asarray(
        [
            1.0,
            last_return,
            *lag_returns,
            *momentum,
            *mean_returns,
            *volatility,
            range_fraction,
            volume_z,
            rsi_scaled,
            math.sin(hour_angle),
            math.cos(hour_angle),
            math.sin(weekday_angle),
            math.cos(weekday_angle),
        ],
        dtype=float,
    )
    if not np.all(np.isfinite(features)):
        raise ValueError("engineered ridge features contain non-finite values")
    return features


def training_examples(
    data: Any,
    horizon: int,
    *,
    min_train_samples: int = DEFAULT_MIN_TRAIN_SAMPLES,
) -> tuple[np.ndarray, np.ndarray, list[int]]:
    """Build direct-horizon examples whose labels are fully observed in ``data``."""
    if horizon not in TARGET_HOURS:
        raise ValueError(f"unsupported horizon: {horizon}")
    closes = np.asarray(data.closes, dtype=float)
    latest_index = len(closes) - 1
    last_training_origin = latest_index - horizon
    indices = list(range(MAX_LAG, last_training_origin + 1))
    if len(indices) < min_train_samples:
        raise ValueError(
            f"insufficient ridge history for {horizon}h: {len(indices)} < {min_train_samples}"
        )
    x = np.vstack([_feature_at(data, index) for index in indices])
    y = np.asarray(
        [math.log(closes[index + horizon] / closes[index]) for index in indices],
        dtype=float,
    )
    return x, y, indices


def _fit_predict_ridge(
    x: np.ndarray,
    y: np.ndarray,
    latest_features: np.ndarray,
    *,
    alpha: float,
) -> float:
    if alpha <= 0:
        raise ValueError("ridge alpha must be positive")
    intercept = x[:, :1]
    continuous = x[:, 1:]
    means = np.mean(continuous, axis=0)
    stds = np.std(continuous, axis=0)
    stds = np.where(stds > 1e-9, stds, 1.0)
    x_scaled = np.concatenate([intercept, (continuous - means) / stds], axis=1)
    latest_scaled = np.concatenate([latest_features[:1], (latest_features[1:] - means) / stds])
    penalty = np.eye(x_scaled.shape[1], dtype=float) * alpha
    penalty[0, 0] = 0.0
    coefficients = np.linalg.solve(x_scaled.T @ x_scaled + penalty, x_scaled.T @ y)
    return float(latest_scaled @ coefficients)


def ridge_feature_forecast(
    data: Any,
    *,
    alpha: float = DEFAULT_RIDGE_ALPHA,
    min_train_samples: int = DEFAULT_MIN_TRAIN_SAMPLES,
) -> dict[str, dict[str, float]]:
    """Forecast 2h/4h/8h/16h BTC prices with direct ridge regressions."""
    closes = np.asarray(data.closes, dtype=float)
    if len(closes) <= MAX_LAG + max(TARGET_HOURS):
        raise ValueError("market window is too short for ridge forecasting")
    current_price = float(closes[-1])
    latest_features = _feature_at(data, len(closes) - 1)
    recent_returns = np.diff(np.log(closes[-73:]))
    recent_volatility = max(float(np.std(recent_returns)), 1e-5)

    result: dict[str, dict[str, float]] = {}
    for horizon in TARGET_HOURS:
        x, y, _ = training_examples(data, horizon, min_train_samples=min_train_samples)
        predicted_return = _fit_predict_ridge(x, y, latest_features, alpha=alpha)
        # Guard against an unstable extrapolation while keeping the model's
        # relative signal. The bound scales with recent realized volatility.
        return_limit = max(0.005, 4.0 * recent_volatility * math.sqrt(horizon))
        predicted_return = float(np.clip(predicted_return, -return_limit, return_limit))
        predicted_price = current_price * math.exp(predicted_return)
        result[f"{horizon}h"] = {
            "price_usd": predicted_price,
            "predicted_log_return": predicted_return,
            "training_samples": float(len(y)),
        }
    return result


def augment_baselines(
    baseline_function: Any,
    data: Any,
    *,
    enabled: bool,
) -> dict[str, dict[str, dict[str, float]]]:
    """Return ordinary baselines plus the ridge member when research/approved."""
    baselines = dict(baseline_function(data))
    if enabled:
        install_adaptive_prior()
        baselines[MODEL_NAME] = ridge_feature_forecast(data)
    return baselines
