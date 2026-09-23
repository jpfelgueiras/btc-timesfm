"""Research-only retargeting API placeholders.

The functions below currently return their input unchanged (``effect=noop``).
They are not evaluated, do not claim economic utility or calibrated retargeting,
and must not be enabled in production absent a separate preregistered study.
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
