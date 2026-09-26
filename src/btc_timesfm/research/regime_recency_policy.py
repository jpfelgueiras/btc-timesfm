"""Bounded, research-only regime and elapsed-recency policy primitives (#348).

Nothing in this module is wired into production forecasting. Synthetic examples
exercise chronology and pooling mechanics only; they are not skill evidence.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from btc_timesfm.data.regime_detection import validated_regime
from btc_timesfm.research.elapsed_recency import elapsed_time_weights, effective_sample_size

POLICY_CATALOG = (
    "unconditioned_equal_family",
    "origin_time_regime",
    "elapsed_decay_inner_selected_half_life",
    "elapsed_decay_persistence_first_fallback",
)


def label_origin_regimes(origins: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Label origin feature snapshots independently, never using future outcomes.

    Rows must be strictly chronological, have integer epoch-second timestamps,
    and provide an origin-time ``features`` mapping. Missing origins are allowed;
    their absence does not get filled by interpolation.
    """
    labels: list[dict[str, Any]] = []
    previous: int | None = None
    for row in origins:
        timestamp = int(row["origin_timestamp"])
        if previous is not None and timestamp <= previous:
            raise ValueError("origin timestamps must be strictly increasing")
        features = row.get("features")
        if not isinstance(features, Mapping):
            raise ValueError("each origin requires an origin-time features mapping")
        labels.append({"origin_timestamp": timestamp, "regime": validated_regime(dict(features))})
        previous = timestamp
    return labels


def elapsed_decay_pool(
    observations: Sequence[Mapping[str, Any]],
    *,
    available_at: int,
    half_life_hours: float,
    minimum_ess: float,
    fallback_value: float | None = None,
) -> dict[str, Any]:
    """Pool matured scalar observations with elapsed-time decay and ESS gating.

    An observation is visible only when its ``available_at`` is no later than
    the forecast cutoff. Insufficient effective sample size returns the explicit
    fallback (or no estimate), rather than a deceptively precise pooled value.
    """
    if not math.isfinite(minimum_ess) or minimum_ess < 0:
        raise ValueError("minimum_ess must be finite and nonnegative")
    visible = [row for row in observations if int(row["available_at"]) <= available_at]
    weights = elapsed_time_weights(
        [int(row["available_at"]) for row in visible],
        available_at=available_at,
        half_life_hours=half_life_hours,
    )
    ess = effective_sample_size(weights)
    total = sum(weights)
    estimate = (
        sum(weight * float(row["value"]) for weight, row in zip(weights, visible, strict=True))
        / total
        if total > 0
        else None
    )
    enough = ess >= minimum_ess
    return {
        "estimate": estimate if enough else fallback_value,
        "raw_estimate": estimate,
        "effective_sample_size": ess,
        "observations": len(visible),
        "used_fallback": not enough,
        "status": "pooled" if enough else "fallback_or_insufficient_evidence",
    }


def build_report(
    *,
    eligible_corpus: bool = False,
    frozen_base_policy: bool = False,
    matured_paired_outcomes: bool = False,
    nested_walk_forward: bool = False,
) -> dict[str, Any]:
    """Return an explicit eligibility report; never infer results from primitives."""
    gates = {
        "eligible_immutable_corpus": eligible_corpus,
        "frozen_post_345_base_policy": frozen_base_policy,
        "matured_paired_outcomes": matured_paired_outcomes,
        "nested_purged_walk_forward": nested_walk_forward,
    }
    missing = [name for name, passed in gates.items() if not passed]
    return {
        "schema_version": 1,
        "experiment": "roadmap_v8_issue_348_regime_elapsed_recency",
        "status": "blocked" if missing else "inconclusive",
        "decision": "blocked" if missing else "inconclusive",
        "promotion_eligible": False,
        "policy_catalog": list(POLICY_CATALOG),
        "gates": gates,
        "missing_gates": missing,
        "results": None,
        "required_metrics": [
            "per-horizon loss and skill versus unconditioned and persistence",
            "worst powered origin-time regime and temporal-block stability",
            "effective sample size, weight turnover/churn, and recovery lag",
            "paired dependence-aware adjusted confidence intervals",
        ],
        "success_criteria": {
            "minimum_loss_reduction_pct": 3.0,
            "adjusted_confidence_interval_lower_bound_gt_zero": True,
            "maximum_powered_regime_regression_pct": 5.0,
            "stable_temporal_blocks": True,
        },
        "note": "No corpus/base-policy evidence means blocked; synthetic primitive tests are not skill evidence.",
    }
