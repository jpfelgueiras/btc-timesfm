#!/usr/bin/env python3
"""Leakage-safe walk-forward analysis for pre-registered feature interactions.

Instead of searching every feature pair, each hypothesis in the catalog is
explicit, versioned, and bounded: it names the two feature groups it combines,
documents the economic rationale, and specifies a fixed rule (a multiplicative
interaction term built from standardized features, or a boolean regime gate).

Every interaction is evaluated against the baseline feature set with the exact
same leakage-safe walk-forward folds used by the family ablations. A
deterministic paired bootstrap reports the incremental metric delta and its
uncertainty, and an interaction is promoted only when the out-of-sample
evidence is stable (candidate better, enough samples, and a majority of folds
improved).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from btc_timesfm.forecasting.statistical_significance import paired_bootstrap_comparison

INTERACTION_SCHEMA_VERSION = 1
REPORT_PATH = Path("feature_interaction_analysis_report.json")
SUMMARY_PATH = Path("feature_interaction_analysis_summary.md")
HORIZONS = (2, 4, 8, 16)
BOOTSTRAP_ITERATIONS = 2000
DEFAULT_BOOTSTRAP_SEED_BASE = 700
DEFAULT_MIN_TRAIN = 24
DEFAULT_PROMOTION_MIN_SAMPLES = 32
DEFAULT_MIN_STABLE_FOLDS = 2
DEFAULT_STABILITY_MAJORITY = 0.5
DEFAULT_DAYS = 30
DEFAULT_SAMPLES = 96
DEFAULT_FOLDS = 4

BASE_FEATURE_NAMES = (
    "volatility_24h_pct",
    "range_24h_avg_pct",
    "volume_zscore_7d",
    "rsi_14",
    "momentum_6h_pct",
    "momentum_24h_pct",
    "momentum_7d_pct",
)

# Feature groups referenced by the interaction catalog. Members map to the
# numeric feature names emitted by the timestamp-safe signal modules. The
# "regime" group is a categorical label and therefore holds no numeric members.
FEATURE_GROUP_MEMBERS: dict[str, tuple[str, ...]] = {
    "funding": ("derivatives_funding_rate_pct",),
    "trend": ("momentum_6h_pct", "momentum_24h_pct", "momentum_7d_pct"),
    "open_interest": (
        "derivatives_open_interest_usd",
        "derivatives_oi_change_1h_pct",
        "derivatives_oi_change_24h_pct",
    ),
    "volatility": ("volatility_24h_pct", "volatility_7d_pct"),
    "order_book_imbalance": (
        "microstructure_imbalance_10bps",
        "microstructure_imbalance_25bps",
        "microstructure_microprice_deviation_bps",
    ),
    "momentum": ("momentum_6h_pct", "momentum_24h_pct"),
    "eth_relative_strength": (
        "cross_eth_btc_relative_6h_pct",
        "cross_eth_btc_relative_24h_pct",
    ),
    "regime": (),
}

# The catalog is deliberately small and fixed: exhaustive combinatorial search
# over feature pairs is avoided by construction. Bump INTERACTION_SCHEMA_VERSION
# (and the resulting catalog sha256) whenever a definition changes.
INTERACTION_CATALOG: list[dict[str, Any]] = [
    {
        "interaction_id": "funding_x_trend",
        "name": "Funding × Trend",
        "description": "Perpetual funding rate modulated by directional BTC trend strength.",
        "feature_a": "funding",
        "feature_b": "trend",
        "rationale": (
            "Positive funding in an up-trend and negative funding in a down-trend both mark "
            "positioning aligned with price; the multiplicative term isolates crowded, potentially "
            "reversible positioning beyond the additive funding effect."
        ),
        "rule": {"type": "product", "feature_a_group": "funding", "feature_b_group": "trend"},
    },
    {
        "interaction_id": "open_interest_x_volatility",
        "name": "Open Interest × Volatility",
        "description": "Derivatives open-interest growth gated by realized volatility expansion.",
        "feature_a": "open_interest",
        "feature_b": "volatility",
        "rationale": (
            "Rising open interest is most predictive when volatility is already expanding, because it "
            "marks new active positioning rather than passive rolling exposure."
        ),
        "rule": {
            "type": "product",
            "feature_a_group": "open_interest",
            "feature_b_group": "volatility",
        },
    },
    {
        "interaction_id": "order_book_imbalance_x_momentum",
        "name": "Order-Book Imbalance × Momentum",
        "description": "Persisted order-book imbalance acting on directional BTC momentum.",
        "feature_a": "order_book_imbalance",
        "feature_b": "momentum",
        "rationale": (
            "Book imbalance is a short-horizon flow signal; combining it with momentum asks whether "
            "flow pressure confirms the prevailing direction."
        ),
        "rule": {
            "type": "product",
            "feature_a_group": "order_book_imbalance",
            "feature_b_group": "momentum",
        },
    },
    {
        "interaction_id": "eth_relative_strength_x_btc_regime",
        "name": "ETH Relative Strength × BTC Regime",
        "description": "ETH-vs-BTC relative strength activated only inside a trending BTC regime.",
        "feature_a": "eth_relative_strength",
        "feature_b": "regime",
        "rationale": (
            "Crypto beta is most informative when BTC itself is already trending; the pre-registered "
            "regime gate avoids fitting beta across heterogeneous market states."
        ),
        "rule": {
            "type": "regime_gate",
            "feature_group": "eth_relative_strength",
            "gate": {"feature": "regime", "values": ("trending",)},
        },
    },
]


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _json_safe(value: Any) -> Any:
    """Recursively normalize tuples/sequences so JSON round trips are identity."""
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def catalog_sha256(interactions: Sequence[Mapping[str, Any]] | None = None) -> str:
    """Deterministic content hash over the explicit interaction definitions."""
    entries = list(interactions if interactions is not None else INTERACTION_CATALOG)
    return hashlib.sha256(_canonical_json(entries).encode("utf-8")).hexdigest()


def catalog_signature() -> dict[str, Any]:
    return {
        "schema_version": INTERACTION_SCHEMA_VERSION,
        "catalog_sha256": catalog_sha256(),
        "interaction_count": len(INTERACTION_CATALOG),
    }


def eligible_training_indices(
    origin_indices: Sequence[int], current_origin_s: int, horizon_hours: int
) -> list[int]:
    """Only rows whose target has already matured by ``current_origin_s`` may train."""
    return [
        index
        for index, origin_s in enumerate(origin_indices)
        if int(origin_s) + horizon_hours * 3600 <= int(current_origin_s)
    ]


def _validate_interaction(interaction: Mapping[str, Any]) -> None:
    required = (
        "interaction_id",
        "name",
        "description",
        "feature_a",
        "feature_b",
        "rationale",
        "rule",
    )
    for key in required:
        if key not in interaction:
            raise ValueError(f"interaction catalog entry is missing '{key}'")
    rule = interaction["rule"]
    if not isinstance(rule, Mapping):
        raise ValueError("interaction rule must be a mapping")
    rule_type = rule.get("type")
    if rule_type == "product":
        for group_key in ("feature_a_group", "feature_b_group"):
            group = rule.get(group_key)
            if group not in FEATURE_GROUP_MEMBERS or not FEATURE_GROUP_MEMBERS[group]:
                raise ValueError(f"product rule references unknown numeric group: {group!r}")
    elif rule_type == "regime_gate":
        group = rule.get("feature_group")
        if group not in FEATURE_GROUP_MEMBERS or not FEATURE_GROUP_MEMBERS[group]:
            raise ValueError(f"regime gate references unknown numeric group: {group!r}")
        gate = rule.get("gate")
        if (
            not isinstance(gate, Mapping)
            or not isinstance(gate.get("feature"), str)
            or not gate.get("values")
        ):
            raise ValueError("regime gate needs a label feature and non-empty values")
    else:
        raise ValueError(f"unsupported interaction rule type: {rule_type!r}")


def _required_feature_names(interaction: Mapping[str, Any]) -> set[str]:
    names = set(BASE_FEATURE_NAMES)
    rule = interaction["rule"]
    if rule["type"] == "product":
        names.update(FEATURE_GROUP_MEMBERS[rule["feature_a_group"]])
        names.update(FEATURE_GROUP_MEMBERS[rule["feature_b_group"]])
    elif rule["type"] == "regime_gate":
        names.update(FEATURE_GROUP_MEMBERS[rule["feature_group"]])
        names.add(rule["gate"]["feature"])
    return names


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number)


def _row_usable(
    features_frame: Mapping[str, Sequence[Any]],
    index: int,
    interaction: Mapping[str, Any],
) -> bool:
    rule = interaction["rule"]
    gate_label = (
        rule["gate"]["feature"]
        if rule["type"] == "regime_gate" and isinstance(rule.get("gate"), Mapping)
        else None
    )
    for name in _required_feature_names(interaction):
        if name == gate_label:
            continue
        series = features_frame.get(name)
        if series is None or index >= len(series):
            return False
        value = series[index]
        if value is None:
            return False
        if isinstance(value, (str, bool)):
            return False
        if not _is_finite_number(value):
            return False
    if gate_label is not None:
        labels = features_frame.get(gate_label)
        if labels is None or index >= len(labels):
            return False
        return labels[index] is not None and str(labels[index]) != ""
    return True


def _standardize(values: np.ndarray, train_mask: np.ndarray) -> np.ndarray:
    train = values[train_mask]
    mean = float(np.mean(train)) if train.size else 0.0
    scale = float(np.std(train)) if train.size else 0.0
    if not math.isfinite(scale) or scale < 1e-9:
        scale = 1.0
    return (values - mean) / scale


def _numeric_vector(series: Sequence[Any], size: int) -> tuple[np.ndarray, np.ndarray]:
    values: np.ndarray = np.zeros(size, dtype=float)
    usable: np.ndarray = np.ones(size, dtype=bool)
    for index in range(size):
        value = series[index]
        if not _is_finite_number(value):
            usable[index] = False
        else:
            values[index] = float(value)
    return values, usable


def _group_composite(
    features_frame: Mapping[str, Sequence[Any]],
    members: Sequence[str],
    size: int,
    train_mask: np.ndarray,
) -> np.ndarray:
    """Equal-weight z-composite over a group's members (train-fold statistics only)."""
    if len(members) == 1:
        values, usable = _numeric_vector(features_frame[members[0]], size)
        return np.where(usable, _standardize(values, train_mask), np.nan)
    zs: list[np.ndarray] = []
    usable = np.ones(size, dtype=bool)
    for member in members:
        values, member_usable = _numeric_vector(features_frame[member], size)
        usable &= member_usable
        zs.append(_standardize(values, train_mask))
    composite = np.mean(np.stack(zs, axis=0), axis=0)
    return np.where(usable, composite, np.nan)


def _interaction_series(
    interaction: Mapping[str, Any],
    features_frame: Mapping[str, Sequence[Any]],
    size: int,
    train_mask: np.ndarray,
) -> np.ndarray:
    """Compute a row-local interaction term using only train-fold statistics."""
    rule = interaction["rule"]
    if rule["type"] == "product":
        side_a = _group_composite(
            features_frame, FEATURE_GROUP_MEMBERS[rule["feature_a_group"]], size, train_mask
        )
        side_b = _group_composite(
            features_frame, FEATURE_GROUP_MEMBERS[rule["feature_b_group"]], size, train_mask
        )
        usable = np.isfinite(side_a) & np.isfinite(side_b)
        return np.where(usable, side_a * side_b, np.nan)
    if rule["type"] == "regime_gate":
        values = _group_composite(
            features_frame, FEATURE_GROUP_MEMBERS[rule["feature_group"]], size, train_mask
        )
        gate = rule["gate"]
        gate_values = {str(value) for value in gate["values"]}
        labels = features_frame[gate["feature"]]
        activate = np.array([str(label) in gate_values for label in labels], dtype=bool)
        return np.where(activate, np.where(np.isfinite(values), values, 0.0), 0.0)
    raise ValueError(f"unsupported interaction rule type: {rule['type']!r}")


def _ridge_predict(train_x: np.ndarray, train_y: np.ndarray, test_x: np.ndarray) -> float:
    means = np.mean(train_x, axis=0)
    scales = np.std(train_x, axis=0, ddof=0)
    scales = np.where(scales < 1e-9, 1.0, scales)
    standardized = (train_x - means) / scales
    test_standardized = (test_x - means) / scales
    design = np.column_stack([np.ones(len(standardized)), standardized])
    penalty: np.ndarray = np.eye(design.shape[1], dtype=float)
    penalty[0, 0] = 0.0
    coefficients = np.linalg.solve(design.T @ design + penalty, design.T @ train_y)
    return float(np.dot(np.concatenate([[1.0], test_standardized]), coefficients))


def _assert_no_future_targets(
    train_indices: Sequence[int],
    origin_indices: Sequence[int],
    test_index: int,
    horizon_hours: int,
) -> None:
    for train_index in train_indices:
        if origin_indices[train_index] + horizon_hours * 3600 > origin_indices[test_index]:
            raise ValueError(
                "fold leaks future targets into training: a training row matures after its test origin"
            )


def promotion_status_from_evidence(
    *,
    conclusion: str,
    samples: int,
    fold_count: int,
    improvement_fraction: float | None,
    promotion_min_samples: int,
    min_stable_folds: int,
) -> str:
    """Promotion requires candidate-better evidence, enough samples, and stability."""
    if samples < promotion_min_samples:
        return "insufficient_evidence"
    stable = (
        fold_count >= min_stable_folds
        and improvement_fraction is not None
        and improvement_fraction > DEFAULT_STABILITY_MAJORITY
    )
    if conclusion == "candidate_better" and stable:
        return "promoted"
    if conclusion == "baseline_better":
        return "rejected"
    return "research_only"


def _promotion_recommendation(status: str) -> str:
    return {
        "promoted": "promote",
        "rejected": "reject",
        "research_only": "continue_research",
        "insufficient_evidence": "insufficient_evidence",
    }[status]


def _mean(values: Sequence[float]) -> float | None:
    return float(np.mean(values)) if values else None


def evaluate_interaction(
    interaction: Mapping[str, Any],
    features_frame: Mapping[str, Sequence[Any]],
    targets: Mapping[str, Sequence[float]],
    origin_indices: Sequence[int],
    folds: Sequence[Mapping[str, Any]],
    *,
    horizons: Sequence[int] = HORIZONS,
    min_train: int = DEFAULT_MIN_TRAIN,
    promotion_min_samples: int = DEFAULT_PROMOTION_MIN_SAMPLES,
    min_stable_folds: int = DEFAULT_MIN_STABLE_FOLDS,
    seed_base: int = DEFAULT_BOOTSTRAP_SEED_BASE,
) -> dict[str, Any]:
    """Evaluate one interaction against the baseline with leakage-safe folds."""
    _validate_interaction(interaction)
    interaction_id = str(interaction["interaction_id"])
    interaction_feature = f"interaction_{interaction_id}"
    missing = sorted(_required_feature_names(interaction) - set(features_frame))
    size = len(origin_indices)
    if size == 0:
        raise ValueError("origin_indices must not be empty")

    for horizon in horizons:
        if f"{horizon}h" not in targets or len(targets[f"{horizon}h"]) != size:
            raise ValueError(f"targets['{horizon}h'] must match the origin row count")
    for name in features_frame:
        if len(features_frame[name]) != size:
            raise ValueError(
                f"feature '{name}' has length {len(features_frame[name])}, expected {size}"
            )

    by_horizon: dict[str, Any] = {}
    for horizon in horizons:
        horizon_hours = int(horizon)
        target_key = f"{horizon}h"
        target_series = targets[target_key]
        outcomes: list[dict[str, Any]] = []
        fold_deltas: list[list[float]] = []
        skipped_insufficient_train = 0
        origins: list[str] = []
        has_origin_labels = "origin_at" in features_frame

        for fold in folds:
            test_indices = [int(index) for index in fold["test"]]
            fold_delta: list[float] = []
            for test_index in test_indices:
                if test_index < 0 or test_index >= size:
                    raise ValueError(f"fold test index {test_index} is out of range")
                if missing or not _row_usable(features_frame, test_index, interaction):
                    continue
                if not _is_finite_number(target_series[test_index]):
                    continue
                if "train" in fold:
                    train_indices = [int(index) for index in fold["train"]]
                    _assert_no_future_targets(
                        train_indices, origin_indices, test_index, horizon_hours
                    )
                else:
                    train_indices = eligible_training_indices(
                        origin_indices, int(origin_indices[test_index]), horizon_hours
                    )
                train_indices = [
                    index
                    for index in train_indices
                    if not missing and _row_usable(features_frame, index, interaction)
                ]
                if len(train_indices) < min_train:
                    skipped_insufficient_train += 1
                    continue

                train_mask: np.ndarray = np.zeros(size, dtype=bool)
                for index in train_indices:
                    train_mask[index] = True

                base_train = np.asarray(
                    [
                        [float(features_frame[name][index]) for name in BASE_FEATURE_NAMES]
                        for index in train_indices
                    ],
                    dtype=float,
                )
                train_y = np.asarray(
                    [float(target_series[index]) for index in train_indices], dtype=float
                )
                test_base = np.asarray(
                    [float(features_frame[name][test_index]) for name in BASE_FEATURE_NAMES],
                    dtype=float,
                )
                interaction_series = _interaction_series(
                    interaction, features_frame, size, train_mask
                )
                interaction_train = np.asarray(interaction_series[train_indices], dtype=float)
                interaction_test = float(interaction_series[test_index])
                if not math.isfinite(interaction_test):
                    continue
                candidate_train = np.column_stack([base_train, interaction_train])
                candidate_test = np.asarray([*test_base, interaction_test], dtype=float)

                baseline_prediction = _ridge_predict(base_train, train_y, test_base)
                candidate_prediction = _ridge_predict(candidate_train, train_y, candidate_test)
                actual = float(target_series[test_index])
                candidate_error = abs(candidate_prediction - actual)
                baseline_error = abs(baseline_prediction - actual)
                outcomes.append(
                    {
                        "baseline_error": baseline_error,
                        "candidate_error": candidate_error,
                        "baseline_direction": (baseline_prediction > 0) == (actual > 0),
                        "candidate_direction": (candidate_prediction > 0) == (actual > 0),
                    }
                )
                fold_delta.append(candidate_error - baseline_error)
                if has_origin_labels:
                    origins.append(str(features_frame["origin_at"][test_index]))
            if fold_delta:
                fold_deltas.append(fold_delta)

        baseline_errors = [outcome["baseline_error"] for outcome in outcomes]
        candidate_errors = [outcome["candidate_error"] for outcome in outcomes]
        baseline_mae = _mean(baseline_errors)
        candidate_mae = _mean(candidate_errors)
        significance = paired_bootstrap_comparison(
            candidate_errors,
            baseline_errors,
            metric=f"{target_key}_mae_pp",
            lower_is_better=True,
            min_samples=promotion_min_samples,
            iterations=BOOTSTRAP_ITERATIONS,
            seed=seed_base + horizon_hours,
        )
        fold_means = [float(np.mean(deltas)) for deltas in fold_deltas]
        folds_with_improvement = sum(1 for mean_delta in fold_means if mean_delta < 0.0)
        improvement_fraction = folds_with_improvement / len(fold_means) if fold_means else None
        relative_improvement = (
            (float(baseline_mae) - float(candidate_mae)) / float(baseline_mae)
            if baseline_mae not in (None, 0.0) and candidate_mae is not None
            else None
        )
        promotion = promotion_status_from_evidence(
            conclusion=str(significance.get("conclusion", "inconclusive")),
            samples=len(outcomes),
            fold_count=len(fold_means),
            improvement_fraction=improvement_fraction,
            promotion_min_samples=promotion_min_samples,
            min_stable_folds=min_stable_folds,
        )

        by_horizon[target_key] = {
            "walk_forward_samples": len(outcomes),
            "skipped_insufficient_train": skipped_insufficient_train,
            "baseline_mae_pp": round(float(baseline_mae), 6) if baseline_mae is not None else None,
            "candidate_mae_pp": round(float(candidate_mae), 6)
            if candidate_mae is not None
            else None,
            "incremental_delta_pp": (
                round(float(candidate_mae) - float(baseline_mae), 6)
                if baseline_mae is not None and candidate_mae is not None
                else None
            ),
            "incremental_relative_improvement": (
                round(float(relative_improvement), 6) if relative_improvement is not None else None
            ),
            "baseline_direction_accuracy": (
                round(float(np.mean([o["baseline_direction"] for o in outcomes])), 6)
                if outcomes
                else None
            ),
            "candidate_direction_accuracy": (
                round(float(np.mean([o["candidate_direction"] for o in outcomes])), 6)
                if outcomes
                else None
            ),
            "fold_stats": {
                "folds": len(fold_means),
                "folds_with_candidate_improvement": folds_with_improvement,
                "improvement_fraction": (
                    round(float(improvement_fraction), 6)
                    if improvement_fraction is not None
                    else None
                ),
                "stable_oos": bool(
                    len(fold_means) >= min_stable_folds
                    and improvement_fraction is not None
                    and improvement_fraction > DEFAULT_STABILITY_MAJORITY
                ),
            },
            "significance": significance,
            "conclusion": significance.get("conclusion", "inconclusive"),
            "promotion_status": promotion,
            "recommendation": _promotion_recommendation(promotion),
        }
        if has_origin_labels:
            by_horizon[target_key]["origins"] = origins

    improvements = [
        float(item["incremental_relative_improvement"])
        for item in by_horizon.values()
        if item["incremental_relative_improvement"] is not None
    ]
    safe = bool(improvements) and min(improvements) >= -0.05
    significantly_better = sum(
        item["conclusion"] == "candidate_better" for item in by_horizon.values()
    )
    promoted_horizons = [
        key for key, item in by_horizon.items() if item["promotion_status"] == "promoted"
    ]
    rejected_horizons = [
        key for key, item in by_horizon.items() if item["promotion_status"] == "rejected"
    ]
    mean_improvement = float(np.mean(improvements)) if improvements else None
    recommendation = (
        "edge_detected"
        if mean_improvement is not None
        and mean_improvement >= 0.01
        and safe
        and significantly_better >= 1
        else "no_defensible_edge"
        if mean_improvement is not None and mean_improvement < 0
        else "insufficient_evidence"
    )
    promotion = (
        "promoted"
        if len(promoted_horizons) >= 2 and not rejected_horizons
        else "rejected"
        if rejected_horizons
        else "insufficient_evidence"
    )
    return {
        "interaction_id": interaction_id,
        "name": interaction["name"],
        "description": interaction["description"],
        "feature_a": interaction["feature_a"],
        "feature_b": interaction["feature_b"],
        "rationale": interaction["rationale"],
        "rule": _json_safe(interaction["rule"]),
        "interaction_features": [interaction_feature],
        "data_status": "unavailable" if missing else "available",
        "missing_features": missing,
        "feature_names": [interaction_feature],
        "feature_sets": {
            "market_only": list(BASE_FEATURE_NAMES),
            "market_plus_interaction": [*BASE_FEATURE_NAMES, interaction_feature],
        },
        "by_horizon": by_horizon,
        "overall": {
            "mean_relative_mae_improvement": (
                round(mean_improvement, 6) if mean_improvement is not None else None
            ),
            "no_material_horizon_regression": safe,
            "statistically_better_horizons": significantly_better,
            "promoted_horizons": promoted_horizons,
            "rejected_horizons": rejected_horizons,
            "promotion": promotion,
            "recommendation": recommendation,
        },
    }


def build_interaction_analysis_report(
    features_frame: Mapping[str, Sequence[Any]],
    targets: Mapping[str, Sequence[float]],
    origin_indices: Sequence[int],
    folds: Sequence[Mapping[str, Any]],
    *,
    interactions: Sequence[Mapping[str, Any]] = INTERACTION_CATALOG,
    horizons: Sequence[int] = HORIZONS,
    min_train: int = DEFAULT_MIN_TRAIN,
    promotion_min_samples: int = DEFAULT_PROMOTION_MIN_SAMPLES,
    min_stable_folds: int = DEFAULT_MIN_STABLE_FOLDS,
    seed_base: int = DEFAULT_BOOTSTRAP_SEED_BASE,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Aggregate per-interaction results into a versioned, JSON-serializable report."""
    if not interactions:
        raise ValueError("at least one interaction is required")
    sections: dict[str, Any] = {}
    for interaction in interactions:
        interaction_id = str(interaction["interaction_id"])
        sections[interaction_id] = evaluate_interaction(
            interaction,
            features_frame,
            targets,
            origin_indices,
            folds,
            horizons=horizons,
            min_train=min_train,
            promotion_min_samples=promotion_min_samples,
            min_stable_folds=min_stable_folds,
            seed_base=seed_base,
        )

    promoted = sorted(
        key for key, section in sections.items() if section["overall"]["promotion"] == "promoted"
    )
    rejected = sorted(
        key for key, section in sections.items() if section["overall"]["promotion"] == "rejected"
    )
    improvements = [
        float(section["overall"]["mean_relative_mae_improvement"])
        for section in sections.values()
        if section["overall"]["mean_relative_mae_improvement"] is not None
    ]
    feature_names = sorted(
        {feature for section in sections.values() for feature in section["interaction_features"]}
    )
    return {
        "schema_version": INTERACTION_SCHEMA_VERSION,
        "method": "paired_leakage_safe_walk_forward_ridge_interaction_analysis",
        "uses_future_information": False,
        "minimum_training_rows": min_train,
        "promotion_min_samples": promotion_min_samples,
        "min_stable_folds": min_stable_folds,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "catalog_version": {
            "schema_version": INTERACTION_SCHEMA_VERSION,
            "catalog_sha256": catalog_sha256(interactions),
            "interaction_count": len(interactions),
        },
        "catalog": [
            {
                "interaction_id": entry["interaction_id"],
                "name": entry["name"],
                "feature_a": entry["feature_a"],
                "feature_b": entry["feature_b"],
                "rule": _json_safe(entry["rule"]),
            }
            for entry in interactions
        ],
        "evaluation": {
            "horizons": [f"{horizon}h" for horizon in horizons],
            "folds": len(folds),
            "row_count": len(origin_indices),
        },
        "baseline_features": list(BASE_FEATURE_NAMES),
        "feature_names": feature_names,
        "fold_leakage_safe": True,
        "interactions": sections,
        "overall": {
            "promoted_interactions": promoted,
            "rejected_interactions": rejected,
            "recommendation": (
                "edge_detected"
                if promoted
                else "no_defensible_edge"
                if rejected
                else "insufficient_evidence"
            ),
            "mean_relative_mae_improvement": (
                round(float(np.mean(improvements)), 6) if improvements else None
            ),
        },
    }


def render_summary(report: Mapping[str, Any]) -> str:
    catalog_version = report["catalog_version"]
    overall = report["overall"]
    lines = [
        "# Feature interaction analysis",
        "",
        f"- Catalog: schema v{catalog_version['schema_version']} hash "
        f"`{catalog_version['catalog_sha256'][:16]}…` ({catalog_version['interaction_count']} "
        "pre-registered interactions, no combinatorial search)",
        f"- Leakage-safe folds: **{'yes' if report['fold_leakage_safe'] else 'no'}**",
        f"- Promoted interactions: **{', '.join(overall['promoted_interactions']) or 'none'}**",
        f"- Recommendation: **{overall['recommendation']}**",
        "",
    ]
    for interaction_id, section in report["interactions"].items():
        section_overall = section["overall"]
        lines.extend(
            [
                f"## {interaction_id} — {section['name']}",
                f"- {section['description']}",
                f"- Rationale: {section['rationale']}",
                f"- Data: **{section['data_status']}** | "
                f"Promotion: **{section_overall['promotion']}** | "
                f"Recommendation: **{section_overall['recommendation']}**",
                f"- Mean relative improvement: "
                f"**{section_overall['mean_relative_mae_improvement'] * 100:.2f}%**"
                if section_overall["mean_relative_mae_improvement"] is not None
                else "- Mean relative improvement: **n/a**",
                "",
                "| Horizon | Samples | Baseline MAE | +Interaction MAE | Δ (pp) | Relative | Evidence | Status |",
                "| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
            ]
        )
        for horizon, item in section["by_horizon"].items():
            baseline = item["baseline_mae_pp"]
            candidate = item["candidate_mae_pp"]
            delta = item["incremental_delta_pp"]
            relative = item["incremental_relative_improvement"]
            lines.append(
                f"| {horizon} | {item['walk_forward_samples']} | "
                f"{baseline if baseline is not None else '--'} | "
                f"{candidate if candidate is not None else '--'} | "
                f"{delta if delta is not None else '--'} | "
                f"{relative * 100:.2f}% | "
                f"{item['significance'].get('conclusion', 'inconclusive')} | "
                f"{item['promotion_status']} |"
                if relative is not None
                else f"| {horizon} | 0 | -- | -- | -- | -- | inconclusive | "
                f"{item['promotion_status']} |"
            )
        lines.append("")
    lines.extend(
        [
            "Promotion is granted only for candidate-better evidence with enough samples and a "
            "majority of walk-forward folds improved; a promoted research interaction still never "
            "changes production forecasts by itself.",
            "",
        ]
    )
    return "\n".join(lines)


def _chronological_folds(origin_indices: Sequence[int], fold_count: int) -> list[dict[str, Any]]:
    order = sorted(range(len(origin_indices)), key=lambda index: int(origin_indices[index]))
    total = len(order)
    folds: list[dict[str, Any]] = []
    for fold_index in range(fold_count):
        start = fold_index * total // fold_count
        end = (fold_index + 1) * total // fold_count
        if end > start:
            folds.append({"test": order[start:end]})
    return folds or [{"test": list(order)}]


def _build_cli_inputs(
    data: Any,
    derivatives_history: dict[str, list[dict[str, Any]]],
    eth: Any,
    vix: list[dict[str, Any]],
    us10y: list[dict[str, Any]],
    *,
    samples: int = DEFAULT_SAMPLES,
) -> tuple[dict[str, list[Any]], dict[str, list[float]], list[int]]:
    """Build a leakage-safe research frame from timestamp-bounded provider data."""
    from btc_timesfm.data.cross_asset_signals import snapshot_from_inputs
    from btc_timesfm.data.derivatives_signals import snapshot_from_rows
    from btc_timesfm.data.regime_detection import validated_regime
    from btc_timesfm.forecasting.forecast_engine import market_features
    from btc_timesfm.research.backtest import slice_market

    first = 513
    last = len(data.closes) - max(HORIZONS) - 1
    if last <= first:
        raise RuntimeError("Historical window is too small for interaction analysis")
    count = min(max(1, samples), last - first + 1)
    indices = sorted(set(map(int, np.linspace(first, last, num=count, dtype=int))))

    feature_names = sorted(
        set(BASE_FEATURE_NAMES)
        | {name for group in FEATURE_GROUP_MEMBERS.values() for name in group}
        | {"regime"}
    )
    frame: dict[str, list[Any]] = {name: [] for name in feature_names}
    targets: dict[str, list[float]] = {f"{horizon}h": [] for horizon in HORIZONS}
    origin_indices: list[int] = []
    for index in indices:
        context = slice_market(data, index)
        origin_s = int(context.timestamps[-1])
        origin = datetime.fromtimestamp(origin_s, tz=timezone.utc)
        spot = market_features(context)
        derivatives = snapshot_from_rows(
            origin,
            derivatives_history.get("funding", []),
            derivatives_history.get("stats", []),
        ).get("features", {})
        cross = snapshot_from_inputs(origin, context, eth, vix, us10y).get("features", {})
        merged = {**spot, **derivatives, **cross}
        for name in feature_names:
            frame[name].append(merged.get(name))
        frame["regime"][-1] = validated_regime(spot)
        current = float(data.closes[index])
        for horizon in HORIZONS:
            targets[f"{horizon}h"].append(
                (float(data.closes[index + horizon]) / current - 1.0) * 100.0
            )
        origin_indices.append(origin_s)
    return frame, targets, origin_indices


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run leakage-safe feature interaction analysis (#119)"
    )
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    parser.add_argument("--min-train", type=int, default=DEFAULT_MIN_TRAIN)
    parser.add_argument("--promotion-min-samples", type=int, default=DEFAULT_PROMOTION_MIN_SAMPLES)
    parser.add_argument("--folds", type=int, default=DEFAULT_FOLDS)
    parser.add_argument("--json-out", type=Path, default=REPORT_PATH)
    parser.add_argument("--md-out", type=Path, default=SUMMARY_PATH)
    args = parser.parse_args()
    days = min(30, max(24, args.days))

    from btc_timesfm.data.cross_asset_signals import (
        ETH_PAIR,
        US10Y_SERIES,
        VIX_SERIES,
        fetch_fred_series,
        fetch_kraken_pair_hourly,
    )
    from btc_timesfm.data.derivatives_signals import fetch_derivatives_history
    from btc_timesfm.research.backtest import fetch_binance_history

    data = fetch_binance_history(days)
    start = datetime.fromtimestamp(data.timestamps[0], tz=timezone.utc) - timedelta(hours=24)
    end = datetime.fromtimestamp(data.timestamps[-1], tz=timezone.utc)
    derivatives_history = fetch_derivatives_history(start, end)
    eth = fetch_kraken_pair_hourly(ETH_PAIR, 720)
    vix = fetch_fred_series(VIX_SERIES)
    us10y = fetch_fred_series(US10Y_SERIES)

    frame, targets, origin_indices = _build_cli_inputs(
        data,
        derivatives_history,
        eth,
        vix,
        us10y,
        samples=args.samples,
    )
    folds = _chronological_folds(origin_indices, args.folds)
    report = build_interaction_analysis_report(
        frame,
        targets,
        origin_indices,
        folds,
        min_train=max(8, args.min_train),
        promotion_min_samples=args.promotion_min_samples,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    report["research_window_days"] = days
    report["eligible_rows"] = len(origin_indices)
    report["providers"] = [
        "binance_spot",
        "binance_funding",
        "gate_contract_stats",
        "kraken_eth",
        "fred",
    ]
    args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.md_out.write_text(render_summary(report), encoding="utf-8")
    print(args.md_out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
