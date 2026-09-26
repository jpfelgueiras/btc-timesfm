"""Fail-closed audit of forecast policy parity across execution paths."""

from __future__ import annotations

from typing import Any

from btc_timesfm.forecasting.diversified_model import production_enabled
from btc_timesfm.forecasting.forecast_policy import PRODUCTION_POLICY, policy_for


def audit_forecast_policy_parity() -> dict[str, Any]:
    """Report policy identity and whether each research path matches production.

    A path is considered parity-compatible only when behavior-affecting
    configuration is identical and research ridge is disabled. Shadow's
    persistence-backed policy is distinct from the forecast construction policy,
    so its live model configuration cannot be attested by this static audit.
    """
    production = PRODUCTION_POLICY.configuration()
    production_ridge_enabled = production_enabled()
    paths: dict[str, Any] = {}
    blocked: list[str] = []
    for run_type in ("backtest", "optimizer"):
        candidate = policy_for(run_type)
        configuration = candidate.configuration()
        same_behavior = {key: value for key, value in configuration.items() if key != "name"} == {
            key: value for key, value in production.items() if key != "name"
        }
        ridge_disabled = not candidate.enable_research_ridge
        status = (
            "parity"
            if same_behavior and ridge_disabled and not production_ridge_enabled
            else "blocked_policy_drift"
        )
        paths[run_type] = {
            "status": status,
            "policy_id": candidate.configuration_id,
            "policy": configuration,
            "behavior_matches_production": same_behavior,
            "research_ridge_enabled": candidate.enable_research_ridge,
            "execution_wiring": {
                "backtest": (
                    "direct adaptive_model_weights override; policy config alone "
                    "is not execution proof"
                ),
                "optimizer": (
                    "replay calls adaptive_weighting directly; policy is not "
                    "installed on replay path"
                ),
            }[run_type],
        }
        if status != "parity":
            blocked.append(run_type)

    paths["shadow"] = {
        "status": "blocked_unattested",
        "reason": (
            "shadow uses a separate persistence-backed policy; forecast configuration "
            "is not attested here"
        ),
        "research_ridge_enabled": None,
    }
    blocked.append("shadow")
    return {
        "status": "blocked" if blocked else "parity",
        "production_policy_id": PRODUCTION_POLICY.configuration_id,
        "production_policy": production,
        "production_research_ridge_enabled": production_ridge_enabled,
        "paths": paths,
        "blocked_paths": blocked,
        "historical_policy_comparison": "unavailable_without_versioned_historical_manifests",
        "historical_skill_claim": "not_made",
    }
