"""Research-only persistence-first policy candidates for Roadmap v5."""

from __future__ import annotations

import math
from typing import Mapping

PERSISTENCE = "persistence"


def normalize_candidate_weights(
    raw_weights: Mapping[str, float], *, max_weight: float = 1.0
) -> dict[str, float]:
    """Normalize nonnegative weights with zero-weight exclusion and no floor.

    Unlike the production bounded normalizer, this research helper permits a
    candidate to assign all mass to persistence and permits unhelpful members
    to receive exactly zero. ``max_weight`` remains an optional family/member
    cap; infeasible caps fail explicitly rather than silently violating it.
    """
    if not math.isfinite(max_weight) or not 0.0 < max_weight <= 1.0:
        raise ValueError("max_weight must be finite and in (0, 1]")
    positive: dict[str, float] = {}
    for name, value in raw_weights.items():
        weight = float(value)
        if not math.isfinite(weight) or weight < 0.0:
            raise ValueError("candidate weights must be finite and nonnegative")
        if weight > 0.0:
            positive[str(name)] = weight
    if not positive:
        raise ValueError("at least one candidate weight must be positive")
    if max_weight * len(positive) < 1.0 - 1e-12:
        raise ValueError("max_weight is infeasible for the positive-weight members")

    # Water-fill proportional scores under the cap. Zero weights stay absent.
    result = {name: 0.0 for name in raw_weights}
    remaining = set(positive)
    remaining_mass = 1.0
    while remaining:
        denominator = sum(positive[name] for name in remaining)
        capped = {
            name for name in remaining if remaining_mass * positive[name] / denominator > max_weight
        }
        if not capped:
            for name in remaining:
                result[name] = remaining_mass * positive[name] / denominator
            break
        for name in capped:
            result[name] = max_weight
            remaining_mass -= max_weight
        remaining -= capped
    return result


def candidate_policy_weights(
    model_names: list[str], frozen_production_weights: Mapping[str, float]
) -> dict[str, dict[str, float]]:
    """Return a fixed catalog of at most eight unfit policy variants.

    The catalog is descriptive, not a selection rule. Any learned blend must
    be fit inside inner chronological folds and evaluated on untouched outer
    folds by the caller.
    """
    names = list(dict.fromkeys(model_names))
    if not names:
        raise ValueError("model_names must not be empty")
    if PERSISTENCE not in names:
        raise ValueError("persistence baseline is required")
    if set(frozen_production_weights) != set(names):
        raise ValueError("frozen production weights must cover exactly the model names")

    family_members = [name for name in names if name != PERSISTENCE]
    equal = {name: 1.0 for name in names}
    groups: dict[str, list[str]] = {}
    for name in names:
        family_name = "timesfm" if name.startswith("timesfm_") else name
        groups.setdefault(family_name, []).append(name)
    family = {
        name: 1.0 / len(groups) / len(members) for members in groups.values() for name in members
    }
    policies = {
        "frozen_production": normalize_candidate_weights(frozen_production_weights),
        "persistence_only": {PERSISTENCE: 1.0},
        "equal_weight": normalize_candidate_weights(equal),
        "family_equal": family,
    }
    for shrink in (0.25, 0.50, 0.75):
        raw = {name: (1.0 - shrink) * family[name] for name in family_members}
        raw[PERSISTENCE] = (1.0 - shrink) * family[PERSISTENCE] + shrink
        policies[f"persistence_shrink_{shrink:.2f}"] = normalize_candidate_weights(raw)
    return policies
