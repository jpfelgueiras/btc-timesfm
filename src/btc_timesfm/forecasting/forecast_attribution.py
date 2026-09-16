"""Machine-readable forecast explanation and attribution (#127).

For every production forecast the engine combines model predictions, adaptive
weights, a regime label, market/derivatives/microstructure/cross-asset features,
calibrated direction probabilities, dynamic no-edge thresholds and the abstention
policy decision into a single forecast JSON. This module reduces that state to a
concise attribution section that explains *why the forecast looks the way it
does* using measured model evidence only.

The wording rules are strict:

- explanations describe model evidence (weights, matured sample counts, measured
  edge, agreement) and never assert causal market narratives;
- no claim promises a guaranteed or even likely outcome;
- every historical-edge statement carries its sample-size context so a sparse
  cohort is visibly sparse instead of looking authoritative.

The section is deterministic: the same inputs produce the same JSON, and every
numeric claim can be traced back to the durable-history-backed model that
produced it.
"""

from __future__ import annotations

import math
from typing import Any

FORECAST_ATTRIBUTION_VERSION = 1
HORIZONS = ("2h", "4h", "8h", "16h")
DOMINANT_MODEL_WEIGHT = 0.34
TOP_CONTRIBUTOR_LIMIT = 3
REGIME_DRIVER_NAMES = (
    "volatility_24h_pct",
    "volatility_7d_pct",
    "momentum_24h_pct",
    "rsi_14",
)
FEATURE_GROUP_PREFIXES: dict[str, tuple[str, ...]] = {
    "derivatives": ("derivatives_",),
    "microstructure": ("microstructure_",),
    "cross_asset": ("cross_", "macro_"),
}
ENGINE_FEATURE_GROUP = "engine"


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _feature_groups(features: dict[str, Any]) -> dict[str, list[str]]:
    """Partition market features into engine/derivatives/microstructure/cross-asset."""
    groups: dict[str, list[str]] = {}
    for name in features:
        for group, prefixes in FEATURE_GROUP_PREFIXES.items():
            if any(str(name).startswith(prefix) for prefix in prefixes):
                groups.setdefault(group, []).append(str(name))
                break
        else:
            groups.setdefault(ENGINE_FEATURE_GROUP, []).append(str(name))
    return {name: sorted(names) for name, names in groups.items() if names}


def explain_regime(regime: str, features: dict[str, Any]) -> dict[str, Any]:
    """Identify which market-feature rule selected the current regime label.

    Mirrors ``forecast_engine.detect_regime`` so the attribution reproduces the
    exact decision the engine made, including the measured feature values.
    """
    features = _dict(features)
    vol24 = _finite_float(features.get("volatility_24h_pct")) or 0.0
    vol7d = max(_finite_float(features.get("volatility_7d_pct")) or 0.0, 1e-6)
    mom24 = abs(_finite_float(features.get("momentum_24h_pct")) or 0.0)
    rsi = _finite_float(features.get("rsi_14"))

    drivers: list[dict[str, Any]] = []
    rule = ""
    if regime == "high_volatility":
        rule = (
            "24h realized volatility exceeds the 7d realized volatility by more "
            "than 35% (volatility_24h_pct > volatility_7d_pct * 1.35)"
        )
        drivers = [
            {
                "feature": "volatility_24h_pct",
                "value": _round(vol24, 4),
                "role": "current short-window realized volatility",
            },
            {
                "feature": "volatility_7d_pct",
                "value": _round(vol7d, 4),
                "role": "baseline realized volatility",
            },
        ]
    elif regime == "trending":
        momentum_trigger = mom24 > max(1.0, vol24 * math.sqrt(24) * 1.2)
        rsi_trigger = rsi is not None and (rsi >= 70 or rsi <= 30)
        triggers: list[str] = []
        if momentum_trigger:
            triggers.append("24h momentum is above the volatility-scaled trend threshold")
        if rsi_trigger:
            triggers.append("RSI-14 is at or beyond a 70/30 extreme")
        rule = " or ".join(triggers) or "trending label selected without a measured trigger"
        if momentum_trigger:
            drivers.append(
                {
                    "feature": "momentum_24h_pct",
                    "value": _round(mom24, 4),
                    "role": "24h momentum magnitude",
                }
            )
        if rsi_trigger:
            drivers.append(
                {
                    "feature": "rsi_14",
                    "value": _round(rsi, 2),
                    "role": "14-period relative strength",
                }
            )
    else:
        rule = (
            "no volatility or momentum extreme exceeded its threshold; steady "
            "conditions are classified as range"
        )
        for name in REGIME_DRIVER_NAMES:
            value = _finite_float(features.get(name))
            drivers.append(
                {
                    "feature": name,
                    "value": _round(value, 4),
                    "role": "measured value used by the range rule",
                }
            )

    return {
        "regime": regime,
        "rule": rule,
        "drivers": drivers,
        "driving_feature_group": ENGINE_FEATURE_GROUP,
    }


def _top_contributors(weights: dict[str, Any]) -> list[dict[str, Any]]:
    ranked = sorted(
        (
            (float(weight), name)
            for name, weight in weights.items()
            if name and isinstance(weight, (int, float)) and float(weight) > 0.0
        ),
        reverse=True,
    )
    contributors: list[dict[str, Any]] = []
    for weight, name in ranked[:TOP_CONTRIBUTOR_LIMIT]:
        contributors.append(
            {
                "model": name,
                "weight": _round(weight, 6),
                "dominant": weight >= DOMINANT_MODEL_WEIGHT,
            }
        )
    return contributors


def _model_metrics(diagnostics: dict[str, Any], weights: dict[str, Any]) -> list[dict[str, Any]]:
    models = _dict(diagnostics.get("models"))
    rows: list[dict[str, Any]] = []
    for name in sorted(weights):
        metric = _dict(models.get(name))
        weight = weights.get(name)
        edge = _finite_float(metric.get("edge_vs_persistence_mae_pct"))
        rows.append(
            {
                "model": name,
                "weight": _round(float(weight), 6) if isinstance(weight, (int, float)) else None,
                "final_weight": _round(_finite_float(metric.get("final_weight")), 6),
                "samples": _int(metric.get("samples")),
                "mae_pct": _round(_finite_float(metric.get("mae_pct"))),
                "direction_accuracy": _round(_finite_float(metric.get("direction_accuracy"))),
                "edge_vs_persistence_mae_pct": _round(edge),
            }
        )
    return rows


def _comparable_conditions(
    regime: str,
    horizon: str,
    weighting_diagnostics: dict[str, Any],
    dynamic_thresholds: dict[str, Any],
    direction_probability: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Historical outcome evidence for the current regime/horizon with sample sizes."""
    dynamic = _dict(_dict(dynamic_thresholds.get("horizons")).get(horizon))
    probability = _dict(_dict(direction_probability.get("horizons")).get(horizon))

    samples = [row["samples"] for row in rows if row["samples"] > 0]
    sample_count = min(samples) if samples else _int(dynamic.get("samples"))
    weighted_edge_parts = [
        float(row["edge_vs_persistence_mae_pct"]) * float(row["weight"])
        for row in rows
        if row["edge_vs_persistence_mae_pct"] is not None and row["weight"] is not None
    ]
    if len(weighted_edge_parts) == len(rows) and rows:
        weighted_edge = sum(weighted_edge_parts)
    else:
        weighted_edge = None

    return {
        "condition": f"{regime}:{horizon}",
        "source": "weighting_diagnostics.models + dynamic_thresholds",
        "samples": sample_count,
        "sample_context": {
            "matured_comparable_samples": sample_count,
            "minimum_reported_weighting_samples": _int(
                _dict(weighting_diagnostics).get("sample_count")
            ),
            "dynamic_threshold_samples": _int(dynamic.get("samples")),
        },
        "models": rows,
        "weighted_edge_vs_persistence_mae_pct": _round(weighted_edge, 6),
        "regime_outcome_stats": {
            "mean_actual_change_pct": _round(_finite_float(dynamic.get("mean_actual_change_pct"))),
            "realized_volatility_pct": _round(
                _finite_float(dynamic.get("realized_volatility_pct"))
            ),
            "directional_agreement_with_fixed_threshold": _round(
                _finite_float(_dict(dynamic.get("baseline_evaluation")).get("agreement_rate"))
            ),
            "learned_neutral_rate": _round(
                _finite_float(_dict(dynamic.get("baseline_evaluation")).get("dynamic_neutral_rate"))
            ),
        },
        "direction_probability": {
            "samples": _int(probability.get("samples")),
            "calibration_state": probability.get("calibration_state"),
            "reliable": probability.get("reliable"),
            "p_up": _round(_finite_float(probability.get("p_up"))),
            "p_down": _round(_finite_float(probability.get("p_down"))),
            "p_move": _round(_finite_float(probability.get("p_move"))),
            "brier_score": _round(_finite_float(probability.get("brier_score"))),
        },
    }


def _edge_state(
    horizon: str,
    abstention_state: str,
    dynamic_thresholds: dict[str, Any],
) -> tuple[str, list[str]]:
    if abstention_state == "forecast_withheld":
        return "withheld", ["forecast is withheld in this evidence state"]
    if abstention_state == "degraded_inputs":
        return "degraded", ["model disagreement or unavailable predictions degrade this horizon"]
    dynamic = _dict(_dict(dynamic_thresholds.get("horizons")).get(horizon))
    status = str(dynamic.get("edge_status") or "unknown")
    if status == "edge":
        if not dynamic.get("suppress_directional_claim", False):
            return "edge", []
        return "suppressed", list(_dict(dynamic).get("suppress_reasons") or [])
    if status == "no_edge":
        return "no_edge", list(_dict(dynamic).get("suppress_reasons") or [])
    if status == "insufficient_evidence":
        return "insufficient_evidence", list(_dict(dynamic).get("suppress_reasons") or [])
    return "unknown", [f"edge evidence for {horizon} is unavailable"]


def _explanation(
    horizon: str,
    prediction: dict[str, Any],
    contributors: list[dict[str, Any]],
    regime_explanation: dict[str, Any],
    edge_state: str,
    comparable: dict[str, Any],
    abstention_reasons: list[str],
) -> str:
    if edge_state == "withheld":
        return (
            f"{horizon}: forecast withheld; the evidence failure state is "
            "reported under abstention."
        )
    parts: list[str] = []
    if edge_state == "degraded":
        parts.append(
            f"{horizon}: model disagreement or missing predictions prevent a healthy claim"
        )
    elif abstention_reasons:
        parts.append(f"{horizon}: forecast is suppressed ({'; '.join(abstention_reasons)})")
    if contributors:
        leader = contributors[0]
        weight_pct = round(float(leader["weight"]) * 100.0, 1)
        parts.append(f"lead ensemble contributor is {leader['model']} at {weight_pct}% weight")
    parts.append(f"regime classified as {regime_explanation['regime']}")
    samples = int(comparable.get("samples") or 0)
    edge = comparable.get("weighted_edge_vs_persistence_mae_pct")
    if edge is not None:
        parts.append(
            f"measured weighted MAE edge versus persistence is {float(edge):+.3f} pct pts across {samples} matured comparable samples"
        )
    else:
        parts.append(f"no comparable matured samples ({samples}) support an edge estimate")
    agreement = _finite_float(prediction.get("model_agreement"))
    if agreement is not None:
        parts.append(f"model agreement is {round(agreement * 100, 0):.0f}%")
    if edge_state == "edge":
        parts.append("the predicted move sits outside the learned no-edge band")
    elif edge_state in {"no_edge", "insufficient_evidence"}:
        parts.append("no measured edge supports a directional claim")
    return "; ".join(parts) + "."


def _horizon_attribution(
    horizon: str,
    predictions: dict[str, Any],
    model_weights: dict[str, Any],
    weighting_diagnostics: dict[str, Any],
    regime: str,
    features: dict[str, Any],
    abstention_policy: dict[str, Any],
    dynamic_thresholds: dict[str, Any],
    direction_probability: dict[str, Any],
) -> dict[str, Any]:
    prediction = _dict(predictions.get(horizon))
    weights = _dict(model_weights.get(horizon))
    diag = _dict(weighting_diagnostics.get(horizon))
    abstention_state = str(_dict(abstention_policy).get("state") or "unknown")
    abstention_reasons = []
    reasons_value = _dict(abstention_policy).get("reasons")
    if isinstance(reasons_value, list):
        abstention_reasons = [str(reason) for reason in reasons_value]

    contributors = _top_contributors(weights)
    rows = _model_metrics(diag, weights)
    comparable = _comparable_conditions(
        regime,
        horizon,
        diag,
        dynamic_thresholds,
        direction_probability,
        rows,
    )
    edge_state, edge_reasons = _edge_state(horizon, abstention_state, dynamic_thresholds)
    local_reasons = list(edge_reasons)
    local_reasons.extend(
        [str(reason) for reason in abstention_reasons if f"{horizon} " in str(reason)]
    )

    agreement = _finite_float(prediction.get("model_agreement"))
    disagreement_pct = _finite_float(prediction.get("model_disagreement_pct"))
    disagreement_usd = _finite_float(prediction.get("model_disagreement_usd"))
    regime_explanation = explain_regime(regime, features)

    return {
        "horizon": horizon,
        "edge_state": edge_state,
        "edge_reasons": local_reasons,
        "dominant_models": [contributor for contributor in contributors if contributor["dominant"]],
        "top_contributors": contributors,
        "model_agreement": {
            "agreement": _round(agreement, 4),
            "model_disagreement_pct": _round(disagreement_pct, 4),
            "model_disagreement_usd": _round(disagreement_usd, 4),
        },
        "regime": regime_explanation,
        "comparable_conditions": comparable,
        "uncertainty": {
            "abstention_state": abstention_state,
            "reasons": abstention_reasons,
        },
        "explanation": _explanation(
            horizon,
            prediction,
            contributors,
            regime_explanation,
            edge_state,
            comparable,
            local_reasons,
        ),
    }


def build_forecast_attribution(
    *,
    predictions: dict[str, Any],
    model_weights: dict[str, Any],
    weighting_diagnostics: dict[str, Any],
    regime: str,
    features: dict[str, Any],
    abstention_policy: dict[str, Any] | None = None,
    dynamic_thresholds: dict[str, Any] | None = None,
    direction_probability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the JSON-serializable ``forecast_attribution`` forecast section.

    The section is generated for every forecast, including withheld/degraded
    ones, so the public JSON always explains the evidence state behind the
    decision (healthy edge claim, no-edge suppression, or withheld evidence
    failure).
    """
    from datetime import datetime, timezone

    predictions = _dict(predictions)
    model_weights = _dict(model_weights)
    weighting_diagnostics = _dict(weighting_diagnostics)
    features = _dict(features)
    abstention = _dict(abstention_policy)
    thresholds = _dict(dynamic_thresholds)
    probability = _dict(direction_probability)

    group_names = _feature_groups(features)
    horizon_results: dict[str, Any] = {}
    edge_states: dict[str, str] = {}
    for horizon in HORIZONS:
        entry = _horizon_attribution(
            horizon,
            predictions,
            model_weights,
            weighting_diagnostics,
            regime,
            features,
            abstention,
            thresholds,
            probability,
        )
        horizon_results[horizon] = entry
        edge_states[horizon] = entry["edge_state"]

    suppressed_horizons = [
        h
        for h, state in edge_states.items()
        if state in {"no_edge", "suppressed", "insufficient_evidence", "withheld", "degraded"}
    ]
    withhold = bool(abstention.get("withhold_forecast"))
    abstention_state = str(abstention.get("state") or "unknown")

    if withhold or abstention_state == "forecast_withheld":
        overall_state = "withheld"
    elif abstention_state == "degraded_inputs":
        overall_state = "degraded"
    elif abstention_state == "low_confidence_no_measurable_edge":
        overall_state = "low_confidence"
    elif suppressed_horizons:
        overall_state = "suppressed"
    else:
        overall_state = "edge"

    agreements = [
        _finite_float(_dict(entry["model_agreement"]).get("agreement"))
        for entry in horizon_results.values()
    ]
    available = [value for value in agreements if value is not None]
    consensus = "mixed" if not available else "agree" if min(available) >= 0.5 else "disagree"

    return {
        "version": FORECAST_ATTRIBUTION_VERSION,
        "meaning": (
            "evidence-based explanation of model weights, regime evidence, "
            "measured edge, and uncertainty for each horizon; descriptions of "
            "model evidence, not causal market narratives or performance claims"
        ),
        "regime": regime,
        "regime_explanation": explain_regime(regime, features),
        "feature_groups": {
            name: {
                "feature_count": len(names),
                "features": names,
            }
            for name, names in sorted(group_names.items())
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "abstention": {
            "state": abstention_state,
            "withhold_forecast": withhold,
            "reasons": abstention.get("reasons")
            if isinstance(abstention.get("reasons"), list)
            else [],
        },
        "overall": {
            "state": overall_state,
            "edge_horizons": sum(1 for state in edge_states.values() if state == "edge"),
            "suppressed_horizons": suppressed_horizons,
            "minimum_model_agreement": (round(min(available), 4) if available else None),
            "model_consensus": consensus,
        },
        "horizons": horizon_results,
        "reproducibility": {
            "source": (
                "build_forecast ensemble outputs + abstention policy + "
                "dynamic thresholds + calibrated direction probabilities"
            ),
            "deterministic": True,
            "dominant_model_weight_threshold": DOMINANT_MODEL_WEIGHT,
            "wording_policy": "evidence-based only; no causal or guaranteed-performance claims",
        },
    }
