"""Decision-aware forecast retargeting for the BTC ensemble.

This module provides retargeting variants to skew point forecasts or interval
boundaries to align with economic objectives, while maintaining calibration.

All retargeting is strictly out-of-sample (no look-ahead).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Tuple


# Retargeting variant signatures:
# Predictions (dict), EconomicValue (data), DirectionProbabilities (data), CalibrationData (data)
# -> (RetargetedPredictions (dict), Metadata (dict))
RetargetingVariant = Callable[
    [Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]],
    Tuple[Dict[str, Any], Dict[str, Any]],
]


def direction_weighted_skew(
    predictions: Dict[str, Any],
    economic_value: Dict[str, Any],
    direction_probabilities: Dict[str, Any],
    calibration_data: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Skew median (q50) toward the direction with higher economic value."""
    # Placeholder implementation
    retargeted = predictions.copy()
    metadata = {"variant": "direction_weighted_skew", "effect": "noop"}
    return retargeted, metadata


def interval_optimization(
    predictions: Dict[str, Any],
    economic_value: Dict[str, Any],
    direction_probabilities: Dict[str, Any],
    calibration_data: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Widen intervals asymmetrically toward higher-cost direction."""
    # Placeholder implementation
    retargeted = predictions.copy()
    metadata = {"variant": "interval_optimization", "effect": "noop"}
    return retargeted, metadata


def abstention_aware_retargeting(
    predictions: Dict[str, Any],
    economic_value: Dict[str, Any],
    direction_probabilities: Dict[str, Any],
    calibration_data: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Suppress low-confidence claims."""
    # Placeholder implementation
    retargeted = predictions.copy()
    metadata = {"variant": "abstention_aware_retargeting", "effect": "noop"}
    return retargeted, metadata
