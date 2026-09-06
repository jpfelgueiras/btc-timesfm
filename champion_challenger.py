#!/usr/bin/env python3
"""Build reproducible champion-vs-challenger evaluation reports."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

REPORT_VERSION = 1
DEFAULT_OPTIMIZER_REPORT = Path("optimizer_report.json")
DEFAULT_PROMOTION_DECISION = Path("promotion_decision.json")
DEFAULT_REPORT_PATH = Path("champion_challenger_report.json")
DEFAULT_SUMMARY_PATH = Path("champion_challenger_summary.md")
HORIZONS = ("2h", "4h", "8h", "16h")


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def configuration_manifest(candidate: Mapping[str, Any], role: str) -> dict[str, Any]:
    parameters = candidate.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {}
    identity_payload = {
        "name": str(candidate.get("name", "unknown")),
        "parameters": parameters,
    }
    identity = _sha256_text(_canonical_json(identity_payload))[:16]
    return {
        "role": role,
        "name": identity_payload["name"],
        "configuration_id": f"forecast-config-{identity}",
        "parameters": parameters,
    }


def _origins(candidate: Mapping[str, Any]) -> list[str]:
    paired = candidate.get("paired_metrics")
    if not isinstance(paired, dict):
        raise ValueError(f"candidate {candidate.get('name')} is missing paired_metrics")
    origins = paired.get("origins")
    if not isinstance(origins, list) or not all(isinstance(value, str) for value in origins):
        raise ValueError(f"candidate {candidate.get('name')} is missing forecast origins")
    return [str(value) for value in origins]


def _candidate_pair(
    optimizer_report: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates = optimizer_report.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("optimizer report is missing candidates")
    normalized = [item for item in candidates if isinstance(item, dict)]
    champion = next(
        (item for item in normalized if item.get("name") == "production"),
        None,
    )
    if champion is None:
        raise ValueError("optimizer report is missing production champion")
    challengers = [item for item in normalized if item.get("name") != "production"]
    if not challengers:
        raise ValueError("optimizer report has no challenger")
    challenger = min(
        challengers,
        key=lambda item: float(item["objective_mae_pct"]),
    )
    return champion, challenger


def validate_identical_origins(
    optimizer_report: Mapping[str, Any],
) -> dict[str, Any]:
    candidates = optimizer_report.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("optimizer report is missing candidates")
    normalized = [item for item in candidates if isinstance(item, dict)]
    champion = next(
        (item for item in normalized if item.get("name") == "production"),
        None,
    )
    if champion is None:
        raise ValueError("optimizer report is missing production champion")
    reference = _origins(champion)
    if not reference:
        raise ValueError("optimizer report contains no walk-forward origins")

    mismatches = [
        str(candidate.get("name", "unknown"))
        for candidate in normalized
        if _origins(candidate) != reference
    ]
    if mismatches:
        names = ", ".join(mismatches)
        raise ValueError(f"champion/challengers were not evaluated on identical origins: {names}")
    return {
        "pairing_key": "forecast_origin",
        "identical_origins": True,
        "origin_count": len(reference),
        "first_origin": reference[0],
        "last_origin": reference[-1],
        "origins_sha256": _sha256_text(_canonical_json(reference)),
    }


def _metric_block(candidate: Mapping[str, Any]) -> dict[str, Any]:
    by_horizon = candidate.get("by_horizon")
    by_regime = candidate.get("by_regime")
    folds = candidate.get("folds")
    persistence = candidate.get("persistence_by_horizon")
    intervals = candidate.get("interval_diagnostics")
    return {
        "samples": candidate.get("samples"),
        "objective_mae_pct": candidate.get("objective_mae_pct"),
        "mean_direction_accuracy": candidate.get("mean_direction_accuracy"),
        "by_horizon": by_horizon if isinstance(by_horizon, dict) else {},
        "by_regime": by_regime if isinstance(by_regime, dict) else {},
        "folds": folds if isinstance(folds, list) else [],
        "persistence_by_horizon": (persistence if isinstance(persistence, dict) else {}),
        "interval_diagnostics": intervals if isinstance(intervals, dict) else {},
    }


def _comparison(optimizer_report: Mapping[str, Any]) -> dict[str, Any]:
    comparison = optimizer_report.get("comparison")
    return dict(comparison) if isinstance(comparison, dict) else {}


def _policy_decision(
    promotion_decision: Mapping[str, Any] | None,
    challenger_name: str,
) -> dict[str, Any]:
    if promotion_decision is None:
        return {
            "available": False,
            "decision": "unknown",
            "reasons": ["promotion_decision_not_available"],
        }
    candidate = promotion_decision.get("candidate")
    policy_candidate = candidate.get("name") if isinstance(candidate, dict) else None
    if policy_candidate != challenger_name:
        raise ValueError(
            f"promotion decision candidate {policy_candidate!r} does not match "
            f"challenger {challenger_name!r}"
        )
    reasons = promotion_decision.get("reasons")
    return {
        "available": True,
        "decision": promotion_decision.get("decision", "unknown"),
        "reasons": reasons if isinstance(reasons, list) else [],
        "policy_id": promotion_decision.get("policy_id"),
        "policy_version": promotion_decision.get("policy_version"),
        "checks": promotion_decision.get("checks", {}),
        "evidence": promotion_decision.get("evidence", {}),
        "production_health": promotion_decision.get("production_health", {}),
    }


def build_report(
    optimizer_report: Mapping[str, Any],
    promotion_decision: Mapping[str, Any] | None = None,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    champion, challenger = _candidate_pair(optimizer_report)
    pairing = validate_identical_origins(optimizer_report)
    challenger_name = str(challenger.get("name", "unknown"))
    policy = _policy_decision(promotion_decision, challenger_name)
    comparison = _comparison(optimizer_report)
    optimizer_hash = _sha256_text(_canonical_json(optimizer_report))
    decision_hash = (
        _sha256_text(_canonical_json(promotion_decision))
        if promotion_decision is not None
        else None
    )
    identity_payload = {
        "optimizer_sha256": optimizer_hash,
        "decision_sha256": decision_hash,
        "champion": champion.get("parameters", {}),
        "challenger": challenger.get("parameters", {}),
        "origins_sha256": pairing["origins_sha256"],
    }
    comparison_id = _sha256_text(_canonical_json(identity_payload))[:16]

    return {
        "schema_version": REPORT_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "comparison_id": f"champion-challenger-{comparison_id}",
        "inputs": {
            "optimizer_report_sha256": optimizer_hash,
            "promotion_decision_sha256": decision_hash,
            "optimizer_schema_version": optimizer_report.get("schema_version"),
            "optimizer_generated_at": optimizer_report.get("generated_at"),
            "data_source": optimizer_report.get("data_source"),
            "tested_period": optimizer_report.get("tested_period", {}),
            "interval_diagnostics": optimizer_report.get(
                "champion_challenger_interval_diagnostics", {}
            ),
        },
        "pairing": pairing,
        "champion": {
            "manifest": configuration_manifest(champion, "champion"),
            "metrics": _metric_block(champion),
        },
        "challenger": {
            "manifest": configuration_manifest(challenger, "challenger"),
            "metrics": _metric_block(challenger),
        },
        "comparison": comparison,
        "statistical_evidence": comparison.get("significance", {}),
        "policy_recommendation": policy,
        "review_contract": {
            "production_changes_automatic": False,
            "requires_human_review": True,
            "recommendation_only": True,
        },
    }


def _fmt(value: object, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, (int, float)):
        return f"{float(value):.{digits}f}"
    return str(value)


def render_summary(report: Mapping[str, Any]) -> str:
    champion = report["champion"]
    challenger = report["challenger"]
    champion_manifest = champion["manifest"]
    challenger_manifest = challenger["manifest"]
    champion_metrics = champion["metrics"]
    challenger_metrics = challenger["metrics"]
    policy = report["policy_recommendation"]
    pairing = report["pairing"]

    lines = [
        "# Champion vs challenger",
        "",
        (
            f"- Champion: **{champion_manifest['name']}** "
            f"(`{champion_manifest['configuration_id']}`)"
        ),
        (
            f"- Challenger: **{challenger_manifest['name']}** "
            f"(`{challenger_manifest['configuration_id']}`)"
        ),
        f"- Identical walk-forward origins: **{pairing['origin_count']}** ✅",
        f"- Policy recommendation: **{policy['decision']}**",
        "",
        "## Horizon metrics",
        "",
        (
            "| Horizon | Champion MAE | Challenger MAE | Champion bias | "
            "Challenger bias | Champion direction | Challenger direction | "
            "Champion coverage | Challenger coverage | Persistence MAE |"
        ),
        ("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"),
    ]
    for horizon in HORIZONS:
        current = champion_metrics["by_horizon"].get(horizon, {})
        candidate = challenger_metrics["by_horizon"].get(horizon, {})
        persistence = challenger_metrics["persistence_by_horizon"].get(horizon, {})
        lines.append(
            f"| {horizon} | {_fmt(current.get('mae_pct'))}% | "
            f"{_fmt(candidate.get('mae_pct'))}% | "
            f"{_fmt(current.get('mean_signed_error_pct'))}% | "
            f"{_fmt(candidate.get('mean_signed_error_pct'))}% | "
            f"{_fmt(current.get('direction_accuracy'))} | "
            f"{_fmt(candidate.get('direction_accuracy'))} | "
            f"{_fmt(current.get('interval_coverage'))} | "
            f"{_fmt(candidate.get('interval_coverage'))} | "
            f"{_fmt(persistence.get('mae_pct'))}% |"
        )

    lines.extend(
        [
            "",
            "## Fold stability",
            "",
            "| Fold | Champion MAE | Challenger MAE |",
            "| --- | ---: | ---: |",
        ]
    )
    champion_folds = champion_metrics.get("folds", [])
    challenger_folds = challenger_metrics.get("folds", [])
    for current, candidate in zip(
        champion_folds,
        challenger_folds,
        strict=True,
    ):
        lines.append(
            f"| {current.get('fold')} | {_fmt(current.get('mae_pct'))}% | "
            f"{_fmt(candidate.get('mae_pct'))}% |"
        )

    lines.extend(["", "## Regime metrics", ""])
    champion_regimes = champion_metrics.get("by_regime", {})
    challenger_regimes = challenger_metrics.get("by_regime", {})
    regimes = sorted(set(champion_regimes) | set(challenger_regimes))
    if regimes:
        lines.extend(
            [
                (
                    "| Regime | Horizon | Champion MAE | Challenger MAE | "
                    "Champion direction | Challenger direction |"
                ),
                "| --- | --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for regime in regimes:
            current_regime = champion_regimes.get(regime, {})
            candidate_regime = challenger_regimes.get(regime, {})
            for horizon in HORIZONS:
                current = current_regime.get(horizon, {})
                candidate = candidate_regime.get(horizon, {})
                lines.append(
                    f"| {regime} | {horizon} | "
                    f"{_fmt(current.get('mae_pct'))}% | "
                    f"{_fmt(candidate.get('mae_pct'))}% | "
                    f"{_fmt(current.get('direction_accuracy'))} | "
                    f"{_fmt(candidate.get('direction_accuracy'))} |"
                )
    else:
        lines.append("No regime metrics were available.")

    significance = report.get("statistical_evidence")
    lines.extend(["", "## Statistical evidence", ""])
    if isinstance(significance, dict) and significance:
        vs_production = significance.get("candidate_vs_production", {})
        vs_persistence = significance.get("candidate_vs_persistence", {})
        production_mae = vs_production.get("mae_pct", {}) if isinstance(vs_production, dict) else {}
        persistence_mae = (
            vs_persistence.get("mae_pct", {}) if isinstance(vs_persistence, dict) else {}
        )
        lines.append(
            f"- Challenger vs champion MAE: **{production_mae.get('conclusion', 'inconclusive')}**"
        )
        lines.append(
            "- Challenger vs persistence MAE: "
            f"**{persistence_mae.get('conclusion', 'inconclusive')}**"
        )
    else:
        lines.append("- Statistical evidence unavailable.")

    lines.extend(["", "## Promotion policy", ""])
    reasons = policy.get("reasons", [])
    lines.append(f"- Decision: **{policy.get('decision', 'unknown')}**")
    if policy.get("policy_id"):
        lines.append(f"- Policy: `{policy['policy_id']}`")
    if isinstance(reasons, list):
        for reason in reasons:
            lines.append(f"- `{reason}`")
    lines.extend(
        [
            "",
            (
                "This report is review-only. It does not change production "
                "parameters or merge configuration changes."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build champion-vs-challenger evaluation report")
    parser.add_argument(
        "--optimizer-report",
        type=Path,
        default=DEFAULT_OPTIMIZER_REPORT,
    )
    parser.add_argument(
        "--promotion-decision",
        type=Path,
        default=DEFAULT_PROMOTION_DECISION,
    )
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY_PATH)
    args = parser.parse_args()

    optimizer_report = _read_json(args.optimizer_report)
    promotion_decision = (
        _read_json(args.promotion_decision) if args.promotion_decision.exists() else None
    )
    report = build_report(optimizer_report, promotion_decision)
    args.report.write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    args.summary.write_text(render_summary(report), encoding="utf-8")
    print(args.summary.read_text(encoding="utf-8"))
    print(f"Saved {args.report} and {args.summary}")


if __name__ == "__main__":
    main()
