"""Reproducible BTC market-regime detectors and transition diagnostics.

The production candidate is a deterministic score-based state model using the
validated feature set already emitted by ``forecast_engine.market_features``.
The module keeps the legacy heuristic as an explicit benchmark and includes a
fixed-prototype alternative so regime-method comparisons do not silently tune
on the evaluation period.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Callable

REGIMES = ("range", "trending", "high_volatility")


def _float(features: dict[str, Any], name: str, default: float = 0.0) -> float:
    try:
        value = float(features.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def heuristic_regime(features: dict[str, Any]) -> str:
    """Exact benchmark matching the original forecast-engine heuristic."""
    vol24 = _float(features, "volatility_24h_pct")
    vol7d = max(_float(features, "volatility_7d_pct"), 1e-6)
    mom24 = abs(_float(features, "momentum_24h_pct"))
    rsi = _float(features, "rsi_14", 50.0)
    if vol24 > vol7d * 1.35:
        return "high_volatility"
    if mom24 > max(1.0, vol24 * math.sqrt(24) * 1.2) or rsi >= 70 or rsi <= 30:
        return "trending"
    return "range"


def regime_scores(features: dict[str, Any]) -> dict[str, float]:
    """Return interpretable volatility/trend/range state scores."""
    vol6 = max(_float(features, "volatility_6h_pct"), 1e-6)
    vol24 = max(_float(features, "volatility_24h_pct"), 1e-6)
    vol7d = max(_float(features, "volatility_7d_pct"), 1e-6)
    range24 = max(_float(features, "range_24h_avg_pct"), 0.0)
    volume_z = abs(_float(features, "volume_zscore_7d"))
    rsi = _float(features, "rsi_14", 50.0)
    mom6 = _float(features, "momentum_6h_pct")
    mom24 = _float(features, "momentum_24h_pct")
    mom7d = _float(features, "momentum_7d_pct")

    vol_ratio = vol24 / vol7d
    short_vol_ratio = vol6 / vol24
    high_volatility = (
        0.60 * max(0.0, (vol_ratio - 1.05) / 0.45)
        + 0.25 * max(0.0, (short_vol_ratio - 1.05) / 0.55)
        + 0.10 * max(0.0, (range24 / max(vol24, 0.05) - 1.20) / 1.40)
        + 0.05 * max(0.0, volume_z - 1.25)
    )

    normalized_24 = abs(mom24) / max(0.40, vol24 * math.sqrt(24.0))
    normalized_6 = abs(mom6) / max(0.20, vol6 * math.sqrt(6.0))
    normalized_7d = abs(mom7d) / max(1.0, vol7d * math.sqrt(168.0))
    direction_consistency = float(
        (mom6 > 0 and mom24 > 0 and mom7d > 0)
        or (mom6 < 0 and mom24 < 0 and mom7d < 0)
    )
    rsi_extremity = abs(rsi - 50.0) / 25.0
    trending = (
        0.50 * normalized_24
        + 0.20 * normalized_6
        + 0.10 * normalized_7d
        + 0.12 * direction_consistency
        + 0.08 * rsi_extremity
    )

    # Range is strongest when realized volatility is not expanding and both
    # normalized momentum and RSI displacement are muted.
    range_score = (
        1.0
        - min(0.70, 0.45 * max(0.0, vol_ratio - 0.90))
        - min(0.65, 0.40 * normalized_24)
        - min(0.30, 0.15 * rsi_extremity)
    )
    return {
        "range": max(0.0, range_score),
        "trending": max(0.0, trending),
        "high_volatility": max(0.0, high_volatility),
    }


def validated_regime(features: dict[str, Any]) -> str:
    """Classify one feature row using conservative score thresholds/deadbands."""
    scores = regime_scores(features)
    high = scores["high_volatility"]
    trend = scores["trending"]
    # Volatility gets priority only after a material expansion. The thresholds
    # deliberately leave a deadband around marginal states to reduce churn.
    if high >= 0.78 and high >= trend * 0.92:
        return "high_volatility"
    if trend >= 0.92 and trend >= high * 0.90:
        return "trending"
    return "range"


def prototype_regime(features: dict[str, Any]) -> str:
    """Fixed-centroid alternative resembling a tiny deterministic state cluster."""
    vol24 = max(_float(features, "volatility_24h_pct"), 1e-6)
    vol7d = max(_float(features, "volatility_7d_pct"), 1e-6)
    mom24 = abs(_float(features, "momentum_24h_pct"))
    rsi = _float(features, "rsi_14", 50.0)
    volume_z = abs(_float(features, "volume_zscore_7d"))
    vector = (
        min(3.0, vol24 / vol7d),
        min(3.0, mom24 / max(0.5, vol24 * math.sqrt(24.0))),
        min(2.0, abs(rsi - 50.0) / 25.0),
        min(3.0, volume_z / 2.0),
    )
    prototypes = {
        "range": (0.90, 0.30, 0.30, 0.30),
        "trending": (1.00, 1.35, 1.10, 0.55),
        "high_volatility": (1.75, 0.75, 0.70, 0.90),
    }
    scales = (0.55, 0.70, 0.70, 0.65)

    def distance(center: tuple[float, ...]) -> float:
        return sum(((value - target) / scale) ** 2 for value, target, scale in zip(vector, center, scales, strict=True))

    return min(prototypes, key=lambda name: distance(prototypes[name]))


def smooth_regime_sequence(
    feature_rows: list[dict[str, Any]],
    *,
    detector: Callable[[dict[str, Any]], str] = validated_regime,
    confirmation_samples: int = 2,
) -> list[str]:
    """Apply optional transition confirmation for offline/research sequences."""
    if confirmation_samples < 1:
        raise ValueError("confirmation_samples must be >= 1")
    raw = [detector(features) for features in feature_rows]
    if not raw or confirmation_samples == 1:
        return raw
    stable = raw[0]
    candidate = stable
    candidate_count = 0
    output = [stable]
    for label in raw[1:]:
        if label == stable:
            candidate = stable
            candidate_count = 0
        elif label == candidate:
            candidate_count += 1
            if candidate_count >= confirmation_samples:
                stable = candidate
                candidate_count = 0
        else:
            candidate = label
            candidate_count = 1
        output.append(stable)
    return output


def transition_churn(labels: list[str]) -> dict[str, float | int]:
    """Measure how often a detector changes state across chronological samples."""
    transitions = sum(left != right for left, right in zip(labels, labels[1:]))
    opportunities = max(0, len(labels) - 1)
    return {
        "samples": len(labels),
        "transitions": transitions,
        "transition_rate": transitions / opportunities if opportunities else 0.0,
    }


def compare_regime_methods(feature_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare legacy, score-state and fixed-prototype labels without fitting."""
    methods: dict[str, Callable[[dict[str, Any]], str]] = {
        "heuristic": heuristic_regime,
        "validated_score": validated_regime,
        "fixed_prototype": prototype_regime,
    }
    report: dict[str, Any] = {}
    for name, detector in methods.items():
        raw = [detector(features) for features in feature_rows]
        smoothed = smooth_regime_sequence(feature_rows, detector=detector)
        report[name] = {
            "label_counts": dict(Counter(raw)),
            "raw_churn": transition_churn(raw),
            "confirmed_churn": transition_churn(smoothed),
            "confirmed_label_counts": dict(Counter(smoothed)),
        }
    return report
