#!/usr/bin/env python3
"""Conservative, reproducible promotion policy for optimizer candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


POLICY_VERSION = 1
DEFAULT_OPTIMIZER_REPORT = Path("optimizer_report.json")
DEFAULT_HEALTH_STATE = Path(".state/pipeline_health.json")
DEFAULT_DECISION_PATH = Path("promotion_decision.json")
DEFAULT_SUMMARY_PATH = Path("promotion_summary.md")
HORIZONS = ("2h", "4h", "8h", "16h")


@dataclass(frozen=True)
class PromotionPolicy:
    minimum_samples: int = 32
    minimum_relative_mae_improvement: float = 0.03
    maximum_horizon_relative_degradation: float = 0.05
    maximum_direction_accuracy_drop: float = 0.02
    minimum_improved_folds: int = 2
    maximum_worst_fold_relative_degradation: float = 0.02
    minimum_regime_samples: int = 8
    maximum_regime_horizon_relative_degradation: float = 0.10
    maximum_persistence_horizon_relative_degradation: float = 0.05
    require_significant_improvement_vs_production: bool = True
    reject_significantly_worse_than_persistence: bool = True
    require_drift_state_none_for_review: bool = True
    reject_severe_drift: bool = True
    reject_open_pipeline_circuits: bool = True


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def policy_identity(policy: PromotionPolicy) -> str:
    payload = {"version": POLICY_VERSION, "policy": asdict(policy)}
    return "promotion-policy-" + _sha256_text(_canonical_json(payload))[:16]


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"Missing or invalid numeric value for {name}")
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"Invalid numeric value for {name}: {value!r}") from exc


def _candidate_pair(report: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates = report.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("optimizer report is missing candidates")
    normalized = [item for item in candidates if isinstance(item, dict)]
    production = next((item for item in normalized if item.get("name") == "production"), None)
    if production is None:
        raise ValueError("optimizer report is missing the production candidate")
    alternatives = [item for item in normalized if item.get("name") != "production"]
    if not alternatives:
        raise ValueError("optimizer report has no challenger candidate")
    challenger = min(
        alternatives, key=lambda item: _float(item.get("objective_mae_pct"), "objective_mae_pct")
    )
    return production, challenger


def _relative_improvement(candidate: float, baseline: float) -> float:
    return (baseline - candidate) / baseline if baseline > 0 else 0.0


def _horizon_changes(
    candidate: Mapping[str, Any], production: Mapping[str, Any]
) -> dict[str, float]:
    result: dict[str, float] = {}
    for horizon in HORIZONS:
        candidate_metric = candidate.get("by_horizon", {}).get(horizon, {})
        production_metric = production.get("by_horizon", {}).get(horizon, {})
        candidate_mae = _float(candidate_metric.get("mae_pct"), f"candidate {horizon} MAE")
        production_mae = _float(production_metric.get("mae_pct"), f"production {horizon} MAE")
        result[horizon] = _relative_improvement(candidate_mae, production_mae)
    return result


def _fold_changes(candidate: Mapping[str, Any], production: Mapping[str, Any]) -> list[float]:
    candidate_folds = candidate.get("folds")
    production_folds = production.get("folds")
    if not isinstance(candidate_folds, list) or not isinstance(production_folds, list):
        raise ValueError("optimizer report is missing fold metrics")
    if len(candidate_folds) != len(production_folds):
        raise ValueError("candidate and production fold counts differ")
    result: list[float] = []
    for index, (candidate_fold, production_fold) in enumerate(
        zip(candidate_folds, production_folds, strict=True), start=1
    ):
        if not isinstance(candidate_fold, dict) or not isinstance(production_fold, dict):
            raise ValueError("invalid fold metric")
        candidate_mae = _float(candidate_fold.get("mae_pct"), f"candidate fold {index} MAE")
        production_mae = _float(production_fold.get("mae_pct"), f"production fold {index} MAE")
        result.append(_relative_improvement(candidate_mae, production_mae))
    return result


def _regime_changes(
    candidate: Mapping[str, Any],
    production: Mapping[str, Any],
    *,
    minimum_samples: int,
) -> dict[str, dict[str, float]]:
    candidate_regimes = candidate.get("by_regime")
    production_regimes = production.get("by_regime")
    if not isinstance(candidate_regimes, dict) or not isinstance(production_regimes, dict):
        return {}
    result: dict[str, dict[str, float]] = {}
    for regime in sorted(set(candidate_regimes) & set(production_regimes)):
        candidate_metrics = candidate_regimes.get(regime)
        production_metrics = production_regimes.get(regime)
        if not isinstance(candidate_metrics, dict) or not isinstance(production_metrics, dict):
            continue
        horizon_changes: dict[str, float] = {}
        for horizon in HORIZONS:
            candidate_metric = candidate_metrics.get(horizon)
            production_metric = production_metrics.get(horizon)
            if not isinstance(candidate_metric, dict) or not isinstance(production_metric, dict):
                continue
            candidate_samples = int(candidate_metric.get("samples", 0))
            production_samples = int(production_metric.get("samples", 0))
            if min(candidate_samples, production_samples) < minimum_samples:
                continue
            candidate_mae_raw = candidate_metric.get("mae_pct")
            production_mae_raw = production_metric.get("mae_pct")
            if candidate_mae_raw is None or production_mae_raw is None:
                continue
            horizon_changes[horizon] = _relative_improvement(
                float(candidate_mae_raw), float(production_mae_raw)
            )
        if horizon_changes:
            result[regime] = horizon_changes
    return result


def _persistence_changes(candidate: Mapping[str, Any]) -> dict[str, float]:
    result: dict[str, float] = {}
    by_horizon = candidate.get("by_horizon", {})
    persistence = candidate.get("persistence_by_horizon", {})
    for horizon in HORIZONS:
        candidate_metric = by_horizon.get(horizon, {})
        persistence_metric = persistence.get(horizon, {})
        candidate_mae = _float(candidate_metric.get("mae_pct"), f"candidate {horizon} MAE")
        persistence_mae = _float(persistence_metric.get("mae_pct"), f"persistence {horizon} MAE")
        result[horizon] = _relative_improvement(candidate_mae, persistence_mae)
    return result


def _significance(report: Mapping[str, Any]) -> tuple[str, str]:
    comparison = report.get("comparison")
    if not isinstance(comparison, dict):
        return "inconclusive", "inconclusive"
    significance = comparison.get("significance")
    if not isinstance(significance, dict):
        return "inconclusive", "inconclusive"
    vs_production = significance.get("candidate_vs_production")
    vs_persistence = significance.get("candidate_vs_persistence")
    production_mae = vs_production.get("mae_pct") if isinstance(vs_production, dict) else None
    persistence_mae = vs_persistence.get("mae_pct") if isinstance(vs_persistence, dict) else None
    production_conclusion = (
        str(production_mae.get("conclusion", "inconclusive"))
        if isinstance(production_mae, dict)
        else "inconclusive"
    )
    persistence_conclusion = (
        str(persistence_mae.get("conclusion", "inconclusive"))
        if isinstance(persistence_mae, dict)
        else "inconclusive"
    )
    return production_conclusion, persistence_conclusion


def health_snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "available": False,
            "drift_severity": "unknown",
            "open_circuits": [],
            "overall_health": "unknown",
        }
    payload = _read_json(path)
    signals = payload.get("current_signals")
    stages = payload.get("stages")
    drift = (
        str(signals.get("drift_severity", "unknown")) if isinstance(signals, dict) else "unknown"
    )
    open_circuits: list[str] = []
    degraded = False
    if isinstance(stages, dict):
        for name, item in stages.items():
            if not isinstance(item, dict):
                continue
            if item.get("circuit_state") == "open":
                open_circuits.append(str(name))
            if item.get("health") == "degraded" or item.get("circuit_state") == "half_open":
                degraded = True
    overall = "open" if open_circuits else "degraded" if degraded else "healthy"
    return {
        "available": True,
        "state_version": payload.get("version"),
        "state_updated_at": payload.get("updated_at"),
        "drift_severity": drift,
        "open_circuits": sorted(open_circuits),
        "overall_health": overall,
    }


def evaluate_promotion(
    optimizer_report: Mapping[str, Any],
    *,
    health: Mapping[str, Any] | None = None,
    policy: PromotionPolicy | None = None,
) -> dict[str, Any]:
    active = policy or PromotionPolicy()
    production, challenger = _candidate_pair(optimizer_report)
    health_info = dict(health or health_snapshot(DEFAULT_HEALTH_STATE))

    production_mae = _float(production.get("objective_mae_pct"), "production objective MAE")
    challenger_mae = _float(challenger.get("objective_mae_pct"), "challenger objective MAE")
    relative_mae_improvement = _relative_improvement(challenger_mae, production_mae)
    direction_delta = _float(
        challenger.get("mean_direction_accuracy"), "challenger direction accuracy"
    ) - _float(production.get("mean_direction_accuracy"), "production direction accuracy")
    horizon_changes = _horizon_changes(challenger, production)
    fold_changes = _fold_changes(challenger, production)
    regime_changes = _regime_changes(
        challenger, production, minimum_samples=active.minimum_regime_samples
    )
    persistence_changes = _persistence_changes(challenger)
    production_significance, persistence_significance = _significance(optimizer_report)

    worst_horizon = min(horizon_changes.values()) if horizon_changes else 0.0
    worst_fold = min(fold_changes) if fold_changes else 0.0
    improved_folds = sum(value > 0 for value in fold_changes)
    evaluated_regime_values = [
        value for horizon_values in regime_changes.values() for value in horizon_values.values()
    ]
    worst_regime = min(evaluated_regime_values) if evaluated_regime_values else None
    worst_persistence = min(persistence_changes.values()) if persistence_changes else 0.0

    drift_severity = str(health_info.get("drift_severity", "unknown"))
    open_circuits = health_info.get("open_circuits", [])
    if not isinstance(open_circuits, list):
        open_circuits = []

    hard_veto_checks = {
        "no_material_horizon_regression": worst_horizon
        >= -active.maximum_horizon_relative_degradation,
        "no_material_regime_regression": (
            worst_regime is None
            or worst_regime >= -active.maximum_regime_horizon_relative_degradation
        ),
        "no_material_persistence_regression": (
            worst_persistence >= -active.maximum_persistence_horizon_relative_degradation
        ),
        "direction_accuracy_not_materially_worse": (
            direction_delta >= -active.maximum_direction_accuracy_drop
        ),
        "stable_across_folds": (
            improved_folds >= active.minimum_improved_folds
            and worst_fold >= -active.maximum_worst_fold_relative_degradation
        ),
        "not_significantly_worse_than_persistence": (
            not active.reject_significantly_worse_than_persistence
            or persistence_significance != "baseline_better"
        ),
        "no_severe_drift": not (active.reject_severe_drift and drift_severity == "severe"),
        "no_open_pipeline_circuits": not (
            active.reject_open_pipeline_circuits and bool(open_circuits)
        ),
    }
    review_requirements = {
        "enough_samples": int(challenger.get("samples", 0)) >= active.minimum_samples,
        "material_mae_improvement": relative_mae_improvement
        >= active.minimum_relative_mae_improvement,
        "statistically_supported_vs_production": (
            not active.require_significant_improvement_vs_production
            or production_significance == "candidate_better"
        ),
        "drift_state_stable": (
            not active.require_drift_state_none_for_review or drift_severity == "none"
        ),
        "pipeline_health_known": bool(health_info.get("available", False)),
    }

    hard_failures = [name for name, passed in hard_veto_checks.items() if not passed]
    evidence_failures = [name for name, passed in review_requirements.items() if not passed]
    if hard_failures:
        decision = "reject"
        reasons = [f"hard_veto:{name}" for name in hard_failures]
    elif not evidence_failures:
        decision = "review"
        reasons = ["all_promotion_guardrails_passed"]
    else:
        decision = "keep"
        reasons = [f"requirement_not_met:{name}" for name in evidence_failures]

    policy_payload = asdict(active)
    return {
        "schema_version": 1,
        "policy_version": POLICY_VERSION,
        "policy_id": policy_identity(active),
        "policy": policy_payload,
        "decision": decision,
        "reasons": reasons,
        "candidate": {
            "name": challenger.get("name"),
            "parameters": challenger.get("parameters", {}),
            "samples": challenger.get("samples"),
            "objective_mae_pct": challenger_mae,
            "mean_direction_accuracy": challenger.get("mean_direction_accuracy"),
        },
        "production": {
            "name": production.get("name"),
            "parameters": production.get("parameters", {}),
            "samples": production.get("samples"),
            "objective_mae_pct": production_mae,
            "mean_direction_accuracy": production.get("mean_direction_accuracy"),
        },
        "evidence": {
            "relative_mae_improvement": round(relative_mae_improvement, 8),
            "direction_accuracy_delta": round(direction_delta, 8),
            "horizon_relative_improvement": {
                key: round(value, 8) for key, value in horizon_changes.items()
            },
            "fold_relative_improvement": [round(value, 8) for value in fold_changes],
            "improved_folds": improved_folds,
            "worst_fold_relative_improvement": round(worst_fold, 8),
            "regime_horizon_relative_improvement": {
                regime: {key: round(value, 8) for key, value in values.items()}
                for regime, values in regime_changes.items()
            },
            "worst_evaluated_regime_relative_improvement": (
                round(worst_regime, 8) if worst_regime is not None else None
            ),
            "persistence_horizon_relative_improvement": {
                key: round(value, 8) for key, value in persistence_changes.items()
            },
            "significance_vs_production": production_significance,
            "significance_vs_persistence": persistence_significance,
        },
        "production_health": health_info,
        "checks": {
            "hard_veto": hard_veto_checks,
            "review_requirements": review_requirements,
        },
    }


def render_summary(decision: Mapping[str, Any]) -> str:
    candidate = decision.get("candidate", {})
    evidence = decision.get("evidence", {})
    health = decision.get("production_health", {})
    lines = [
        "# Optimizer promotion policy",
        "",
        f"- Decision: **{str(decision.get('decision', 'unknown')).upper()}**",
        f"- Candidate: **{candidate.get('name', 'unknown')}**",
        f"- Policy: `{decision.get('policy_id')}`",
        f"- Relative mean MAE improvement: **{float(evidence.get('relative_mae_improvement', 0.0)) * 100:.2f}%**",
        f"- Production drift state: **{str(health.get('drift_severity', 'unknown')).upper()}**",
        f"- Open production circuits: **{', '.join(health.get('open_circuits', [])) or 'none'}**",
        "",
        "## Hard vetoes",
        "",
    ]
    checks = decision.get("checks", {})
    hard_veto = checks.get("hard_veto", {}) if isinstance(checks, Mapping) else {}
    if isinstance(hard_veto, Mapping):
        for name, passed in hard_veto.items():
            lines.append(f"- {'✅' if passed else '❌'} `{name}`")
    lines.extend(["", "## Review requirements", ""])
    review = checks.get("review_requirements", {}) if isinstance(checks, Mapping) else {}
    if isinstance(review, Mapping):
        for name, passed in review.items():
            lines.append(f"- {'✅' if passed else '❌'} `{name}`")
    lines.extend(
        [
            "",
            "A **REVIEW** decision only makes the candidate eligible for human review. ",
            "This policy never changes production parameters or merges configuration changes automatically.",
            "",
        ]
    )
    return "\n".join(lines)


def build_decision_report(
    optimizer_path: Path,
    *,
    health_path: Path = DEFAULT_HEALTH_STATE,
    policy: PromotionPolicy | None = None,
) -> dict[str, Any]:
    optimizer_text = optimizer_path.read_text(encoding="utf-8")
    optimizer_report = json.loads(optimizer_text)
    if not isinstance(optimizer_report, dict):
        raise ValueError("optimizer report must contain a JSON object")
    health = health_snapshot(health_path)
    decision = evaluate_promotion(optimizer_report, health=health, policy=policy)
    decision.update(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "optimizer_report_sha256": _sha256_text(optimizer_text),
            "optimizer_report_schema_version": optimizer_report.get("schema_version"),
            "optimizer_generated_at": optimizer_report.get("generated_at"),
            "optimizer_recommendation": optimizer_report.get("recommendation"),
        }
    )
    return decision


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate optimizer output against promotion policy"
    )
    parser.add_argument("--optimizer-report", type=Path, default=DEFAULT_OPTIMIZER_REPORT)
    parser.add_argument("--health-state", type=Path, default=DEFAULT_HEALTH_STATE)
    parser.add_argument("--output", type=Path, default=DEFAULT_DECISION_PATH)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY_PATH)
    args = parser.parse_args()

    decision = build_decision_report(
        args.optimizer_report,
        health_path=args.health_state,
    )
    args.output.write_text(json.dumps(decision, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.summary.write_text(render_summary(decision), encoding="utf-8")
    print(args.summary.read_text(encoding="utf-8"))
    print(f"Saved {args.output} and {args.summary}")


if __name__ == "__main__":
    main()
