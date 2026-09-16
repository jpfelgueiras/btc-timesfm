#!/usr/bin/env python3
"""Generate review-only rollback recommendations from post-promotion live evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


POLICY_VERSION = 1
HORIZONS = ("2h", "4h", "8h", "16h")
DEFAULT_PROMOTION_DECISION = Path("promotion_decision.json")
DEFAULT_CHAMPION_REPORT = Path("champion_challenger_report.json")
DEFAULT_LIVE_REPORT = Path("live_promotion_report.json")
DEFAULT_STATE = Path(".state/rollback_safeguard_state.json")
DEFAULT_OUTPUT = Path("rollback_recommendation.json")
DEFAULT_SUMMARY = Path("rollback_summary.md")


@dataclass(frozen=True)
class RollbackPolicy:
    minimum_samples: int = 32
    consecutive_breaches_required: int = 2
    maximum_relative_mae_degradation: float = 0.05
    maximum_direction_accuracy_drop: float = 0.02
    maximum_calibration_error_increase: float = 0.05
    maximum_protected_horizon_relative_mae_degradation: float = 0.05


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def policy_identity(policy: RollbackPolicy) -> str:
    return (
        "rollback-policy-"
        + _sha256_text(_canonical_json({"version": POLICY_VERSION, "policy": asdict(policy)}))[:16]
    )


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"missing or invalid numeric value for {name}")
    return float(value)


def _configuration(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} configuration is missing")
    result = dict(value)
    if not result.get("configuration_id"):
        raise ValueError(f"{name} configuration_id is missing")
    return result


def retain_previous_champion(
    promotion_decision: Mapping[str, Any], champion_report: Mapping[str, Any]
) -> dict[str, Any]:
    """Create immutable promotion evidence before a reviewed change is deployed."""
    if promotion_decision.get("decision") != "review":
        raise ValueError("only a review-approved promotion can establish a rollback record")
    champion = champion_report.get("champion")
    challenger = champion_report.get("challenger")
    if not isinstance(champion, Mapping) or not isinstance(challenger, Mapping):
        raise ValueError("champion report is missing champion or challenger")
    previous = _configuration(champion.get("manifest"), "previous champion")
    promoted = _configuration(challenger.get("manifest"), "promoted challenger")
    return {
        "schema_version": 1,
        "promotion_policy_id": promotion_decision.get("policy_id"),
        "previous_champion": previous,
        "promoted_configuration": promoted,
        "promotion_decision_sha256": _sha256_text(_canonical_json(promotion_decision)),
        "champion_report_sha256": _sha256_text(_canonical_json(champion_report)),
    }


def build_live_report(db_path: Path, configuration_id: str) -> dict[str, Any]:
    """Build comparable matured live metrics for a promoted configuration and champion."""
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT o.configuration_id, p.horizon_hours, p.absolute_error_pct,
                   p.direction_correct, p.within_q10_q90
            FROM forecast_predictions AS p
            JOIN forecast_origins AS o USING(origin_at)
            WHERE p.model_name = 'ensemble'
              AND p.actual_target_price_usd IS NOT NULL
              AND o.configuration_id IS NOT NULL
            ORDER BY p.target_at
            """
        ).fetchall()
    promoted_rows = [row for row in rows if row["configuration_id"] == configuration_id]
    previous_rows = [row for row in rows if row["configuration_id"] != configuration_id]
    if not promoted_rows or not previous_rows:
        raise ValueError("durable history lacks matured promoted or previous-champion outcomes")

    def metrics(items: list[sqlite3.Row]) -> dict[str, float]:
        coverage = [
            float(row["within_q10_q90"]) for row in items if row["within_q10_q90"] is not None
        ]
        return {
            "mae_pct": sum(float(row["absolute_error_pct"]) for row in items) / len(items),
            "direction_accuracy": sum(float(row["direction_correct"]) for row in items)
            / len(items),
            "calibration_error": abs((sum(coverage) / len(coverage)) - 0.8) if coverage else 0.0,
        }

    report: dict[str, Any] = {
        "schema_version": 1,
        "configuration_id": configuration_id,
        "samples": len(promoted_rows),
        "promoted": metrics(promoted_rows),
        "previous_champion": metrics(previous_rows),
        "by_horizon": {},
    }
    for horizon in HORIZONS:
        hour = int(horizon.rstrip("h"))
        promoted_horizon = [row for row in promoted_rows if row["horizon_hours"] == hour]
        previous_horizon = [row for row in previous_rows if row["horizon_hours"] == hour]
        if promoted_horizon and previous_horizon:
            report["by_horizon"][horizon] = {
                "samples": len(promoted_horizon),
                "promoted_mae_pct": metrics(promoted_horizon)["mae_pct"],
                "previous_champion_mae_pct": metrics(previous_horizon)["mae_pct"],
            }
    return report


def _metric_pair(live: Mapping[str, Any], key: str) -> tuple[float, float]:
    promoted = live.get("promoted")
    previous = live.get("previous_champion")
    if not isinstance(promoted, Mapping) or not isinstance(previous, Mapping):
        raise ValueError("live report must contain promoted and previous_champion metrics")
    return _number(promoted.get(key), f"promoted {key}"), _number(
        previous.get(key), f"previous {key}"
    )


def evaluate_rollback(
    promotion_record: Mapping[str, Any],
    live_report: Mapping[str, Any],
    *,
    prior_state: Mapping[str, Any] | None = None,
    policy: RollbackPolicy | None = None,
) -> dict[str, Any]:
    active = policy or RollbackPolicy()
    previous = _configuration(promotion_record.get("previous_champion"), "previous champion")
    promoted = _configuration(promotion_record.get("promoted_configuration"), "promoted")
    if live_report.get("configuration_id") != promoted["configuration_id"]:
        raise ValueError("live report configuration_id does not match promoted configuration")
    samples = int(live_report.get("samples", 0))
    promoted_mae, previous_mae = _metric_pair(live_report, "mae_pct")
    promoted_direction, previous_direction = _metric_pair(live_report, "direction_accuracy")
    promoted_calibration, previous_calibration = _metric_pair(live_report, "calibration_error")
    relative_mae_degradation = (
        (promoted_mae - previous_mae) / previous_mae if previous_mae > 0 else 0.0
    )
    direction_drop = previous_direction - promoted_direction
    calibration_increase = promoted_calibration - previous_calibration
    horizons = live_report.get("by_horizon", {})
    if not isinstance(horizons, Mapping):
        raise ValueError("live report by_horizon must be an object")
    horizon_degradation: dict[str, float] = {}
    for horizon in HORIZONS:
        item = horizons.get(horizon)
        if not isinstance(item, Mapping):
            continue
        live_mae = _number(item.get("promoted_mae_pct"), f"{horizon} promoted MAE")
        baseline_mae = _number(item.get("previous_champion_mae_pct"), f"{horizon} previous MAE")
        horizon_degradation[horizon] = (
            (live_mae - baseline_mae) / baseline_mae if baseline_mae > 0 else 0.0
        )
    protected_failures = sorted(
        horizon
        for horizon, degradation in horizon_degradation.items()
        if degradation > active.maximum_protected_horizon_relative_mae_degradation
    )
    checks = {
        "enough_live_samples": samples >= active.minimum_samples,
        "no_material_live_skill_regression": relative_mae_degradation
        <= active.maximum_relative_mae_degradation,
        "direction_quality_preserved": direction_drop <= active.maximum_direction_accuracy_drop,
        "calibration_preserved": calibration_increase <= active.maximum_calibration_error_increase,
        "protected_horizons_preserved": not protected_failures,
    }
    breaches = [
        name for name, passed in checks.items() if not passed and name != "enough_live_samples"
    ]
    prior_breaches = prior_state.get("breaches", []) if isinstance(prior_state, Mapping) else []
    prior_count = (
        int(prior_state.get("consecutive_breach_count", 0))
        if isinstance(prior_state, Mapping)
        else 0
    )
    same_breach = sorted(prior_breaches) == sorted(breaches) and bool(breaches)
    breach_count = prior_count + 1 if same_breach else (1 if breaches else 0)
    protected_veto = bool(protected_failures) and checks["enough_live_samples"]
    rollback_ready = (
        checks["enough_live_samples"]
        and bool(breaches)
        and (protected_veto or breach_count >= active.consecutive_breaches_required)
    )
    decision = "recommend_rollback" if rollback_ready else "continue_monitoring"
    return {
        "schema_version": 1,
        "policy_version": POLICY_VERSION,
        "policy_id": policy_identity(active),
        "policy": asdict(active),
        "decision": decision,
        "review_required": True,
        "production_mutation_performed": False,
        "previous_champion": previous,
        "promoted_configuration": promoted,
        "live_evidence": {
            "samples": samples,
            "relative_mae_degradation": round(relative_mae_degradation, 8),
            "direction_accuracy_drop": round(direction_drop, 8),
            "calibration_error_increase": round(calibration_increase, 8),
            "protected_horizon_relative_mae_degradation": {
                key: round(value, 8) for key, value in horizon_degradation.items()
            },
            "protected_horizon_failures": protected_failures,
        },
        "checks": checks,
        "breaches": breaches,
        "consecutive_breach_count": breach_count,
        "rollback_reason": (
            "protected_horizon_veto"
            if protected_veto
            else "sustained_live_degradation"
            if rollback_ready
            else None
        ),
    }


def render_summary(recommendation: Mapping[str, Any]) -> str:
    evidence = recommendation.get("live_evidence", {})
    return "\n".join(
        [
            "# Promotion rollback safeguard",
            "",
            f"- Decision: **{str(recommendation.get('decision', 'unknown')).upper()}**",
            f"- Live samples: **{evidence.get('samples', 'n/a')}**",
            f"- Consecutive breach evaluations: **{recommendation.get('consecutive_breach_count', 0)}**",
            f"- Rollback target: `{recommendation.get('previous_champion', {}).get('configuration_id', 'n/a')}`",
            "",
            "Any rollback remains a review-required configuration change; this safeguard never mutates production or merges a PR.",
            "",
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate post-promotion rollback safeguards")
    parser.add_argument("--promotion-decision", type=Path, default=DEFAULT_PROMOTION_DECISION)
    parser.add_argument("--champion-report", type=Path, default=DEFAULT_CHAMPION_REPORT)
    parser.add_argument("--live-report", type=Path, default=DEFAULT_LIVE_REPORT)
    parser.add_argument("--history-db", type=Path)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    args = parser.parse_args()
    record = retain_previous_champion(
        _read_json(args.promotion_decision), _read_json(args.champion_report)
    )
    live_report = (
        build_live_report(args.history_db, record["promoted_configuration"]["configuration_id"])
        if args.history_db is not None
        else _read_json(args.live_report)
    )
    state = _read_json(args.state) if args.state.exists() else None
    recommendation = evaluate_rollback(record, live_report, prior_state=state)
    recommendation["generated_at"] = datetime.now(timezone.utc).isoformat()
    args.state.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(recommendation, indent=2, sort_keys=True) + "\n"
    args.state.write_text(serialized, encoding="utf-8")
    args.output.write_text(serialized, encoding="utf-8")
    args.summary.write_text(render_summary(recommendation), encoding="utf-8")
    print(args.summary.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
