"""Multiple-testing safeguards for automated forecasting research.

Automated research evaluates many configurations, model variants and feature
hypotheses against the same production baseline. The more variants a search
family tries, the more likely a pure-noise candidate is to clear an otherwise
reasonable single-comparison threshold. This module makes that pressure
explicit: a machine-readable policy maps the family size to a stricter
per-comparison evidence threshold, and the adjusted evidence result is what
the promotion policy consumes. A larger search family therefore can never, on
its own, make a candidate promotable.

The adjusted evidence is produced by re-running the same deterministic paired
bootstrap comparison (``statistical_significance``) at a family-adjusted
confidence level, so the conclusion semantics stay identical to the rest of
the research pipeline.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from btc_timesfm.forecasting.statistical_significance import (
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_CONFIDENCE,
    DEFAULT_MIN_PAIRED_SAMPLES,
    DEFAULT_SEED,
    paired_bootstrap_comparison,
)

from btc_timesfm.research import experiment_registry  # noqa: F401  (documented dependency #122)


POLICY_VERSION = 1
DEFAULT_FAMILY = "optimizer_candidate_variants"
VALID_METHODS = ("bonferroni", "sidak", "holm", "by")


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_float(value: object, name: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"Missing or invalid numeric value for {name}")
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"Invalid numeric value for {name}: {value!r}") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}; got {parsed}")
    return parsed


@dataclass(frozen=True)
class MultipleTestingPolicy:
    """Machine-readable multiple-testing policy.

    method: one of ``bonferroni``, ``sidak``, ``holm`` or ``by``
        (Benjamini-Yekutieli). ``holm`` is the default and controls
        familywise error with a step-down procedure.
    alpha: the accepted family error rate (familywise for
        bonferroni/sidak/holm, false-discovery for ``by``).
    max_comparisons: safety cap on the family size used for adjustment so one
        runaway search run cannot drive the threshold to zero.
    """

    method: str = "holm"
    alpha: float = 0.05
    max_comparisons: int = 1000

    def __post_init__(self) -> None:
        if self.method not in VALID_METHODS:
            raise ValueError(f"invalid method {self.method!r}; expected one of {VALID_METHODS}")
        object.__setattr__(
            self, "alpha", _validate_float(self.alpha, "alpha", minimum=0.0, maximum=1.0)
        )
        if isinstance(self.max_comparisons, bool) or not isinstance(self.max_comparisons, int):
            raise ValueError("max_comparisons must be a positive integer")
        if self.max_comparisons < 1:
            raise ValueError("max_comparisons must be a positive integer")


def default_policy() -> MultipleTestingPolicy:
    return MultipleTestingPolicy()


def policy_to_dict(policy: MultipleTestingPolicy) -> dict[str, Any]:
    return {
        "version": POLICY_VERSION,
        "method": policy.method,
        "alpha": policy.alpha,
        "max_comparisons": policy.max_comparisons,
    }


def policy_from_dict(data: Mapping[str, Any]) -> MultipleTestingPolicy:
    """Build a policy from machine-readable config (e.g. a JSON policy file)."""
    if not isinstance(data, Mapping):
        raise ValueError("multiple-testing policy must be a mapping")
    method = str(data.get("method", "holm"))
    raw_alpha = data.get("alpha", 0.05)
    raw_max = data.get("max_comparisons", 1000)
    policy = MultipleTestingPolicy(
        method=method,
        alpha=_validate_float(raw_alpha, "alpha", minimum=0.0, maximum=1.0),
        max_comparisons=int(raw_max),
    )
    # Validate the alpha bounds are applied to a really-machined numeric value.
    return policy


def policy_identity(policy: MultipleTestingPolicy) -> str:
    payload = {"policy_version": POLICY_VERSION, "policy": policy_to_dict(policy)}
    return "multiple-testing-policy-" + _sha256_text(_canonical_json(payload))[:16]


def family_comparison_count(
    raw_count: int | None,
    *,
    policy: MultipleTestingPolicy,
    family: str = DEFAULT_FAMILY,
    source: str | None = None,
) -> dict[str, Any]:
    """Resolve the effective comparison-family size with the policy cap applied."""
    if raw_count is None or raw_count < 1:
        raw = 1
    else:
        raw = int(raw_count)
    count = min(raw, policy.max_comparisons)
    return {
        "family": family,
        "raw_count": raw,
        "comparisons_considered": count,
        "truncated": raw > policy.max_comparisons,
        "max_comparisons": policy.max_comparisons,
        "source": source or "caller_supplied",
    }


def harmonic_number(n: int) -> float:
    return sum(1.0 / value for value in range(1, max(1, n) + 1))


def per_comparison_alpha(
    method: str,
    alpha: float,
    comparisons: int,
    rank: int = 1,
) -> float:
    """Per-comparison error rate for the given method, family size and rank."""
    count = max(1, int(comparisons))
    position = min(max(1, int(rank)), count)
    if method == "bonferroni":
        return alpha / count
    if method == "sidak":
        return 1.0 - (1.0 - alpha) ** (1.0 / count)
    if method == "holm":
        return alpha / (count - position + 1)
    if method == "by":
        return alpha * position / (count * harmonic_number(count))
    raise ValueError(f"invalid method {method!r}; expected one of {VALID_METHODS}")


def adjusted_p_value(
    method: str,
    p_value: float,
    comparisons: int,
    rank: int = 1,
) -> float:
    """Transparency-adjusted p-value; still informative but not the gate."""
    count = max(1, int(comparisons))
    position = min(max(1, int(rank)), count)
    p = min(1.0, max(0.0, float(p_value)))
    if method == "bonferroni":
        return min(1.0, p * count)
    if method == "sidak":
        return max(0.0, 1.0 - (1.0 - p) ** count)
    if method == "holm":
        return min(1.0, p * (count - position + 1))
    if method == "by":
        return min(1.0, p * harmonic_number(count) * count / position)
    raise ValueError(f"invalid method {method!r}; expected one of {VALID_METHODS}")


def p_value_from_bootstrap(result: Mapping[str, Any]) -> float:
    """One-sided bootstrap p-value for the candidate-better direction."""
    probability = float(result.get("probability_candidate_better") or 0.0)
    return min(1.0, max(1e-6, 1.0 - probability))


def family_rank(p_values: Sequence[float], focal_p_value: float) -> int:
    """One-based rank of the focal comparison (1 = most significant)."""
    return 1 + sum(1 for value in p_values if value < focal_p_value)


def reject_family(
    p_values: Sequence[float], policy: MultipleTestingPolicy | None = None
) -> list[bool]:
    """Apply the whole-family procedure and report which comparisons reject.

    Used for synthetic familywise/false-discovery control tests and as a
    machine-readable helper for auditing a family of raw p-values.
    """
    active = policy or MultipleTestingPolicy()
    raw = [min(1.0, max(0.0, float(value))) for value in p_values]
    count = min(len(raw), active.max_comparisons)
    ranking = sorted(range(len(raw)), key=lambda idx: (raw[idx], idx))
    sorted_p = [raw[idx] for idx in ranking]
    rejected = [False] * len(raw)

    if active.method in ("bonferroni", "sidak"):
        threshold = per_comparison_alpha(active.method, active.alpha, count)
        for position, value in enumerate(sorted_p):
            if position >= count:
                break
            if value <= threshold:
                rejected[ranking[position]] = True
        return rejected
    if active.method == "holm":
        rejected_count = 0
        for position, value in enumerate(sorted_p):
            if position >= count:
                break
            threshold = per_comparison_alpha("holm", active.alpha, count, position + 1)
            if value <= threshold and rejected_count == position:
                rejected[ranking[position]] = True
                rejected_count += 1
            else:
                break
        return rejected
    if active.method == "by":
        harmonic = harmonic_number(count)
        last = -1
        for position, value in enumerate(sorted_p):
            if position >= count:
                break
            if value <= active.alpha * (position + 1) / (count * harmonic):
                last = position
        for position in range(last + 1):
            rejected[ranking[position]] = True
        return rejected
    raise ValueError(f"invalid method {active.method!r}; expected one of {VALID_METHODS}")


def adjusted_bootstrap_comparison(
    candidate: Sequence[float],
    baseline: Sequence[float],
    *,
    metric: str,
    lower_is_better: bool,
    policy: MultipleTestingPolicy | None = None,
    comparisons_considered: int | None = None,
    rank: int = 1,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    seed: int = DEFAULT_SEED,
    min_samples: int = DEFAULT_MIN_PAIRED_SAMPLES,
    family: str = DEFAULT_FAMILY,
) -> dict[str, Any]:
    """Compare paired measurements at a family-adjusted evidence threshold.

    Runs the unadjusted deterministic bootstrap comparison and then re-runs it
    at ``confidence = 1 - per_comparison_alpha`` for this comparison's rank and
    family size. The returned ``conclusion`` is the adjusted one and is what
    promotion logic consumes.
    """
    active = policy or MultipleTestingPolicy()
    family_info = family_comparison_count(comparisons_considered, policy=active, family=family)
    count = family_info["comparisons_considered"]
    alpha_adj = per_comparison_alpha(active.method, active.alpha, count, rank)

    raw = paired_bootstrap_comparison(
        candidate,
        baseline,
        metric=metric,
        lower_is_better=lower_is_better,
        confidence=DEFAULT_CONFIDENCE,
        iterations=iterations,
        min_samples=min_samples,
        seed=seed,
    )
    if count <= 1:
        adjusted = dict(raw)
    else:
        adjusted = paired_bootstrap_comparison(
            candidate,
            baseline,
            metric=metric,
            lower_is_better=lower_is_better,
            confidence=round(1.0 - alpha_adj, 12),
            iterations=iterations,
            min_samples=min_samples,
            seed=seed,
        )
    p_value = p_value_from_bootstrap(raw)
    return {
        "schema_version": 1,
        "comparison_type": "paired_bootstrap",
        "metric": metric,
        "policy_version": POLICY_VERSION,
        "policy_id": policy_identity(active),
        "policy": policy_to_dict(active),
        "family": family_info,
        "comparison": {"rank": rank},
        "per_comparison_alpha": round(alpha_adj, 12),
        "p_value": round(p_value, 8),
        "adjusted_p_value": round(adjusted_p_value(active.method, p_value, count, rank), 8),
        "raw": {
            "confidence": raw["confidence"],
            "samples": raw["samples"],
            "probability_candidate_better": raw["probability_candidate_better"],
            "improvement_ci": raw["improvement_ci"],
            "conclusion": raw["conclusion"],
            "reason": raw["reason"],
        },
        "adjusted": {
            "confidence": adjusted["confidence"],
            "samples": adjusted["samples"],
            "probability_candidate_better": adjusted["probability_candidate_better"],
            "improvement_ci": adjusted["improvement_ci"],
            "conclusion": adjusted["conclusion"],
            "reason": adjusted["reason"],
        },
        "conclusion": adjusted["conclusion"],
        "survives": adjusted["conclusion"] == "candidate_better",
    }


def _pairwise_mae_significance(
    challenger: Mapping[str, Any],
    production: Mapping[str, Any],
    *,
    iterations: int,
    seed: int,
) -> dict[str, Any]:
    challenger_metrics = challenger["paired_metrics"]
    production_metrics = production["paired_metrics"]
    if challenger_metrics["origins"] != production_metrics["origins"]:
        raise ValueError("statistical comparisons require identical forecast origins")
    return paired_bootstrap_comparison(
        challenger_metrics["mae_pct"],
        production_metrics["mae_pct"],
        metric="mae_pct",
        lower_is_better=True,
        iterations=iterations,
        seed=seed,
    )


def build_multiple_testing_assessment(
    optimizer_report: Mapping[str, Any],
    *,
    policy: MultipleTestingPolicy | None = None,
    selected_candidate: str | None = None,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Build the family-aware adjusted evidence for an optimizer report.

    The family of comparisons is the set of challenger configurations that were
    replayed against the frozen production baseline (matching #122's notion of
    a cataloged research family). The selected challenger's adjusted evidence is
    ranked against every challenger's raw p-value so step-down methods (holm,
    ``by``) receive the correct position.
    """
    active = policy or MultipleTestingPolicy()
    candidates = [item for item in optimizer_report.get("candidates", []) if isinstance(item, dict)]
    if not candidates:
        raise ValueError("optimizer report is missing candidates")
    production = next((item for item in candidates if item.get("name") == "production"), None)
    if production is None:
        raise ValueError("optimizer report is missing the production candidate")
    challengers = [item for item in candidates if item.get("name") != "production"]
    if not challengers:
        raise ValueError("optimizer report has no challenger candidates")

    selected: dict[str, Any]
    if selected_candidate is None:
        selected = min(challengers, key=lambda item: float(item["objective_mae_pct"]))
    else:
        matches = [item for item in challengers if item.get("name") == selected_candidate]
        if not matches:
            raise ValueError(f"unknown selected candidate {selected_candidate!r}")
        selected = matches[0]

    p_values: list[float] = []
    for challenger in challengers:
        significance = _pairwise_mae_significance(
            challenger, production, iterations=iterations, seed=seed
        )
        p_values.append(p_value_from_bootstrap(significance))
    focal = p_values[challengers.index(selected)]
    rank = family_rank(p_values, focal)

    assessment = adjusted_bootstrap_comparison(
        selected["paired_metrics"]["mae_pct"],
        production["paired_metrics"]["mae_pct"],
        metric="mae_pct",
        lower_is_better=True,
        policy=active,
        comparisons_considered=len(challengers),
        rank=rank,
        iterations=iterations,
        seed=seed,
    )
    assessment["family"]["raw_count"] = len(challengers)
    assessment["family"]["source"] = "optimizer_report_candidates"
    assessment["selected_candidate"] = str(selected.get("name"))
    assessment["challenger_candidates"] = [str(item.get("name")) for item in challengers]
    return assessment
