"""Blocked, research-only evaluation contract for issue #345.

This report never runs forecasts. Candidate policy families are fixed before
data is available; member-level ablations and ridge require separately
validated inputs and are not inferred from the D2 ledger.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from btc_timesfm.research.persistence_shrinkage import candidate_policy_weights

PLANNED_POLICY_FAMILIES = (
    "persistence",
    "current_production_ensemble",
    "equal_model_and_family_weights",
    "timesfm_only",
    "per_context_delete_one",
    "drift_and_ar_delete_one",
    "fixed_shrinkage_to_persistence",
)
MAX_POLICY_FAMILIES = 8


def build_report(
    *,
    model_names: list[str] | None = None,
    production_weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Build a fail-closed report; this issue report cannot claim corpus eligibility.

    Planned families are always listed; unfit candidate weight vectors are
    included only when their model names and frozen weights are supplied.
    """
    if len(PLANNED_POLICY_FAMILIES) > MAX_POLICY_FAMILIES:
        raise RuntimeError("policy family catalog exceeds its declared bound")
    if (model_names is None) != (production_weights is None):
        raise ValueError("model_names and production_weights must be provided together")
    vectors = (
        candidate_policy_weights(model_names, production_weights)
        if model_names is not None and production_weights is not None
        else {}
    )
    candidates = [
        {
            "name": name,
            "status": "weights_generated_not_evaluated",
            "weights": weights,
            "eligible": False,
        }
        for name, weights in vectors.items()
    ]
    return {
        "schema_version": 1,
        "experiment": "issue-345-timesfm-incremental-skill-persistence-first-ablation",
        "status": "blocked",
        "decision": "no_candidate_passes",
        "corpus": {
            "issue_339_status": "blocked",
            "eligible_origin_count": 0,
            "provided_eligible": False,
            "reason": "canonical frozen D1 corpus has zero eligible coverage",
        },
        "evidence": {
            "d2": {
                "classification": "descriptive_only_not_qualifying",
                "paired_rows": "95 pairs maximum",
                "reported_pattern": "persistence outperformed ensemble at 8h and 16h",
                "acceptance_eligible": False,
            },
            "d1_d3": "unavailable_or_not_mature",
        },
        "planned_policy_families": list(PLANNED_POLICY_FAMILIES),
        "candidate_weights": candidates,
        "additional_ablations": {
            "per_context_delete": "separate matched-origin analysis; preserve failures",
            "drift_ar_delete": "separate matched-origin analysis; preserve failures",
            "adaptive_regime_directional_coverage_correlation": (
                "after base-model parity, ablate each term separately"
            ),
            "ridge": {
                "role": "separate challenger only",
                "eligible": False,
                "status": "blocked",
            },
        },
        "evaluation_contract": {
            "same_exact_origins": True,
            "preserve_failed_forecasts": True,
            "weights_fit_only_on_inner_oof": True,
            "persistence_weight_may_be_one": True,
            "allow_zero_timesfm_or_family_weights": True,
            "success": {
                "minimum_improvement": 0.03,
                "dependence_aware_confidence_interval": True,
                "must_beat_production_persistence_and_strong_naive": True,
                "maximum_horizon_loss": 0.05,
                "maximum_direction_loss_percentage_points": 2,
            },
        },
        "inference_performed": False,
        "forecasts_fabricated": False,
        "production_modified": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("issue_345_report.json"))
    args = parser.parse_args()
    report = build_report()
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Issue #345 status: {report['status']}; report: {args.output}")


if __name__ == "__main__":
    main()
