"""Leakage-safe forecasting baselines used by backtests and research.

The benchmark suite deliberately stays simple. Its purpose is to provide strong,
reproducible reference points that more complex models must beat on identical
forecast origins and horizons.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from forecast_engine import FORECAST_HOURS, MarketData, baseline_forecasts


BENCHMARK_NAMES = (
    "persistence",
    "drift_7d",
    "drift_24h",
    "seasonal_naive_24h",
    "ar1",
    "ema_return_24h",
)


def _safe_std(values: np.ndarray) -> float:
    return float(np.std(values, ddof=1)) if len(values) > 1 else 0.0


def _path_prices(current_price: float, path: np.ndarray) -> dict[str, dict[str, float]]:
    cumulative = np.cumsum(path.astype(np.float64))
    return {
        f"{hour}h": {
            "price_usd": float(current_price * math.exp(float(cumulative[hour - 1])))
        }
        for hour in (2, 4, 8, 16)
    }


def _drift_24h(data: MarketData) -> dict[str, dict[str, float]]:
    returns = data.returns.astype(np.float64)
    current_price = float(data.closes[-1])
    sigma = max(_safe_std(returns[-168:]), 1e-6)
    drift = float(np.mean(returns[-24:])) if len(returns) else 0.0
    drift = float(np.clip(drift, -2.0 * sigma, 2.0 * sigma))
    return _path_prices(current_price, np.full(FORECAST_HOURS, drift, dtype=np.float64))


def _ema_return_24h(data: MarketData) -> dict[str, dict[str, float]]:
    returns = data.returns.astype(np.float64)
    current_price = float(data.closes[-1])
    if not len(returns):
        return _path_prices(current_price, np.zeros(FORECAST_HOURS, dtype=np.float64))

    alpha = 2.0 / 25.0
    window = returns[-168:]
    ema = float(window[0])
    for value in window[1:]:
        ema = alpha * float(value) + (1.0 - alpha) * ema

    sigma = max(_safe_std(window), 1e-6)
    ema = float(np.clip(ema, -2.0 * sigma, 2.0 * sigma))
    return _path_prices(current_price, np.full(FORECAST_HOURS, ema, dtype=np.float64))


def _seasonal_naive_24h(data: MarketData) -> dict[str, dict[str, float]]:
    """Repeat the observed price from the matching hour in the previous day.

    For a forecast made at origin ``t``, horizon ``h`` uses only the value at
    ``t + h - 24``. Since all supported horizons are below 24 hours, every
    referenced value is already known at forecast time.
    """
    closes = data.closes.astype(np.float64)
    current_index = len(closes) - 1
    result: dict[str, dict[str, float]] = {}
    for hour in (2, 4, 8, 16):
        source_index = current_index + hour - 24
        price = float(closes[source_index]) if source_index >= 0 else float(closes[-1])
        result[f"{hour}h"] = {"price_usd": price}
    return result


def benchmark_forecasts(data: MarketData) -> dict[str, dict[str, dict[str, float]]]:
    """Return the complete deterministic benchmark suite for one forecast origin."""
    existing = baseline_forecasts(data)
    benchmarks: dict[str, dict[str, dict[str, float]]] = {
        "persistence": existing["persistence"],
        "drift_7d": existing["drift_7d"],
        "drift_24h": _drift_24h(data),
        "seasonal_naive_24h": _seasonal_naive_24h(data),
        "ar1": existing["ar1"],
        "ema_return_24h": _ema_return_24h(data),
    }
    return benchmarks


def benchmark_metadata() -> dict[str, Any]:
    """Machine-readable description persisted into backtest reports/manifests."""
    return {
        "models": list(BENCHMARK_NAMES),
        "primary_baseline": "persistence",
        "seasonal_period_hours": 24,
        "drift_windows_hours": [24, 168],
        "ema_span_hours": 24,
        "evaluation_rule": "same forecast origins, horizons, regimes and scoring as production models",
    }
