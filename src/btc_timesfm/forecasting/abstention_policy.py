"""Deterministic production forecast abstention policy."""

from __future__ import annotations

from typing import Any

ABSTENTION_POLICY_VERSION = 1
HORIZONS = ("2h", "4h", "8h", "16h")
MIN_MODEL_AGREEMENT = 0.5


def _is_healthy_data(data_health: Any) -> bool:
    if isinstance(data_health, bool):
        return data_health
    if not isinstance(data_health, dict):
        return False
    if isinstance(data_health.get("healthy"), bool):
        return data_health["healthy"]
    return data_health.get("status") in {"healthy", "ok"}


def _model_disagreement(predictions: dict[str, Any]) -> list[str]:
    disagreements: list[str] = []
    for horizon in HORIZONS:
        prediction = predictions.get(horizon)
        if not isinstance(prediction, dict):
            disagreements.append(f"{horizon} prediction is unavailable")
            continue
        try:
            agreement = float(prediction.get("model_agreement"))
        except (TypeError, ValueError):
            disagreements.append(f"{horizon} model agreement is unavailable")
            continue
        if agreement < MIN_MODEL_AGREEMENT:
            disagreements.append(
                f"{horizon} model agreement {agreement:.0%} is below {MIN_MODEL_AGREEMENT:.0%}"
            )
    return disagreements


def build_abstention_policy(
    predictions: dict[str, Any],
    *,
    data_health: Any,
    drift_report: dict[str, Any],
    forecast_confidence: dict[str, Any],
    dynamic_thresholds: dict[str, Any],
    direction_probability: dict[str, Any],
) -> dict[str, Any]:
    """Classify the public forecast state from production evidence available now."""
    predictions = predictions if isinstance(predictions, dict) else {}
    drift_report = drift_report if isinstance(drift_report, dict) else {}
    forecast_confidence = forecast_confidence if isinstance(forecast_confidence, dict) else {}
    dynamic_thresholds = dynamic_thresholds if isinstance(dynamic_thresholds, dict) else {}
    direction_probability = direction_probability if isinstance(direction_probability, dict) else {}

    data_healthy = _is_healthy_data(data_health)
    drift_severity = str(drift_report.get("severity") or "unknown").lower()
    confidence_status = str(forecast_confidence.get("status") or "unknown")
    confidence_label = str(forecast_confidence.get("label") or "unknown")
    directional_claim_allowed = dynamic_thresholds.get("public_directional_claim_allowed") is True
    probability_claim_allowed = direction_probability.get("public_claim_allowed") is True
    disagreements = _model_disagreement(predictions)
    reasons: list[str] = []

    if not data_healthy:
        reasons.append("market data health is degraded")
    if drift_severity == "severe":
        reasons.append("severe production drift is active")
    if reasons:
        state = "forecast_withheld"
    else:
        if confidence_status != "available":
            reasons.append("measured rolling skill or calibration evidence is insufficient")
        elif confidence_label == "low":
            reasons.append("measured rolling skill is low")
        if not directional_claim_allowed:
            reasons.append("no measurable directional edge across all horizons")
        if not probability_claim_allowed:
            reasons.append("calibrated directional probability evidence is insufficient")
        reasons.extend(disagreements)
        if reasons:
            state = "degraded_inputs" if disagreements else "low_confidence_no_measurable_edge"
        else:
            state = "healthy"

    return {
        "version": ABSTENTION_POLICY_VERSION,
        "state": state,
        "withhold_forecast": state == "forecast_withheld",
        "public_directional_claim_allowed": state == "healthy",
        "reasons": reasons,
        "inputs": {
            "market_data_healthy": data_healthy,
            "drift_severity": drift_severity,
            "forecast_confidence_status": confidence_status,
            "forecast_confidence_label": confidence_label,
            "dynamic_directional_claim_allowed": directional_claim_allowed,
            "probability_claim_allowed": probability_claim_allowed,
            "model_disagreement": disagreements,
            "minimum_model_agreement": MIN_MODEL_AGREEMENT,
        },
    }
