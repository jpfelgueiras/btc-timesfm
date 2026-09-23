"""Versioned forecast-policy contracts shared by every execution path.

A policy contains only behavior-affecting settings. Runtime observations, paths,
timestamps, and data-health values deliberately do not participate in its ID.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable

from btc_timesfm.data.regime_detection import validated_regime
from btc_timesfm.forecasting.correlation_weighting import correlation_aware_model_weights
from btc_timesfm.forecasting.diversified_model import augment_baselines, production_enabled

WeightFunction = Callable[..., tuple[dict[str, float], dict[str, Any]]]


@dataclass(frozen=True)
class ForecastPolicy:
    """Immutable description of one complete forecast execution policy."""

    name: str = "production"
    version: int = 1
    correlation_aware_weights: bool = True
    validated_regimes: bool = True
    enable_research_ridge: bool = False
    conformal_intervals: bool = True
    drift_attenuation: bool = True
    coherence: bool = True
    confidence_after_coherence: bool = True
    model_revision: str | None = None

    def configuration(self) -> dict[str, Any]:
        """Return the canonical, behavior-only policy representation."""
        return asdict(self)

    @property
    def configuration_id(self) -> str:
        encoded = json.dumps(
            self.configuration(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        return f"policy-{hashlib.sha256(encoded).hexdigest()[:20]}"

    def research_variant(self, *, name: str, enable_research_ridge: bool = False) -> ForecastPolicy:
        """Return an isolated named policy with explicit research-only changes.

        Replay and optimizer paths default to production parity. A caller must
        opt into an unapproved model rather than receiving it as an import-side
        effect merely because the run is research-oriented.
        """
        return replace(self, name=name, enable_research_ridge=enable_research_ridge)

    def weight_function(self) -> WeightFunction:
        if self.correlation_aware_weights:
            return correlation_aware_model_weights
        from btc_timesfm.forecasting.adaptive_weighting import adaptive_model_weights

        return adaptive_model_weights

    def install(self, target: Any) -> None:
        """Install this policy on a module exposing ``forecast_engine``.

        Installation is idempotent for the same policy. Installing a different
        policy in the same process is rejected so experiments cannot leak into
        subsequent runs through module globals.
        """
        engine = getattr(target, "forecast_engine", target)
        installed = getattr(engine, "_forecast_policy_id", None)
        if installed is not None and installed != self.configuration_id:
            raise RuntimeError(
                f"forecast engine already has policy {installed}; "
                f"cannot install {self.configuration_id} in the same process"
            )

        engine.adaptive_model_weights = self.weight_function()
        if hasattr(target, "adaptive_model_weights"):
            target.adaptive_model_weights = self.weight_function()
        if self.validated_regimes:
            engine.detect_regime = validated_regime

        if not getattr(engine, "_policy_baselines_wrapped", False):
            original = engine.baseline_forecasts
            enabled = self.enable_research_ridge or production_enabled()

            def policy_baselines(data: Any) -> Any:
                return augment_baselines(original, data, enabled=enabled)

            engine.baseline_forecasts = policy_baselines
            engine._policy_baselines_wrapped = True
            engine._ridge_baselines_wrapped = True
            engine._ridge_model_enabled = enabled

        engine._forecast_policy_id = self.configuration_id
        engine._forecast_policy = self.configuration()


PRODUCTION_POLICY = ForecastPolicy()


def policy_for(run_type: str) -> ForecastPolicy:
    """Return the named policy for an execution path."""
    if run_type == "production_forecast":
        return PRODUCTION_POLICY
    if run_type in {"backtest", "optimizer"}:
        return PRODUCTION_POLICY.research_variant(name=run_type, enable_research_ridge=True)
    if run_type == "shadow":
        return PRODUCTION_POLICY
    raise ValueError(f"unknown forecast policy run type: {run_type}")
