#!/usr/bin/env python3
"""Aggregate feature-family ablations into a reproducible selection report."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


REPORT_PATH = Path("feature_selection_report.json")
SUMMARY_PATH = Path("feature_selection_summary.md")
DEFAULT_REPORT_PATHS = {
    "derivatives": Path("derivatives_ablation_report.json"),
    "microstructure": Path("microstructure_ablation_report.json"),
    "cross_asset": Path("cross_asset_ablation_report.json"),
}
SELECTION_VERSION = 1


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _baseline_features(report: Mapping[str, Any]) -> list[str]:
    feature_sets = report.get("feature_sets")
    if isinstance(feature_sets, dict):
        baseline = feature_sets.get("market_only")
        if isinstance(baseline, list) and all(isinstance(item, str) for item in baseline):
            return [str(item) for item in baseline]
    baseline = report.get("baseline_features")
    if isinstance(baseline, list) and all(isinstance(item, str) for item in baseline):
        return [str(item) for item in baseline]
    raise ValueError("feature ablation report is missing baseline features")


def _feature_names(report: Mapping[str, Any]) -> list[str]:
    names = report.get("feature_names")
    if not isinstance(names, list) or not all(isinstance(item, str) for item in names):
        raise ValueError("feature ablation report is missing feature names")
    return [str(item) for item in names]


def _summary_metrics(report: Mapping[str, Any]) -> dict[str, Any]:
    overall = report.get("overall")
    if isinstance(overall, dict):
        return {
            "mean_relative_mae_improvement": overall.get("mean_relative_mae_improvement"),
            "no_material_horizon_regression": overall.get("no_material_horizon_regression"),
            "statistically_better_horizons": overall.get("statistically_better_horizons"),
            "recommendation": overall.get("recommendation", "unknown"),
        }

    horizons = report.get("horizons")
    if not isinstance(horizons, dict) or not horizons:
        raise ValueError("feature ablation report is missing horizon metrics")

    improvements: list[float] = []
    better_horizons = 0
    safe = True
    for item in horizons.values():
        if not isinstance(item, dict):
            continue
        baseline = item.get("baseline_mae_pp")
        candidate = item.get("candidate_mae_pp")
        if (
            isinstance(baseline, (int, float))
            and isinstance(candidate, (int, float))
            and baseline > 0
        ):
            improvement = (float(baseline) - float(candidate)) / float(baseline)
            improvements.append(improvement)
            safe = safe and improvement >= -0.05
        if isinstance(item.get("significance"), dict):
            if item["significance"].get("conclusion") == "candidate_better":
                better_horizons += 1
    mean_improvement = sum(improvements) / len(improvements) if improvements else None
    recommendation = (
        "edge_detected"
        if mean_improvement is not None
        and mean_improvement >= 0.01
        and safe
        and better_horizons >= 1
        else "no_defensible_edge"
        if mean_improvement is not None and mean_improvement < 0
        else "insufficient_evidence"
    )
    return {
        "mean_relative_mae_improvement": mean_improvement,
        "no_material_horizon_regression": safe,
        "statistically_better_horizons": better_horizons,
        "recommendation": recommendation,
    }


def build_feature_selection_report(
    component_reports: Mapping[str, Mapping[str, Any]],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if not component_reports:
        raise ValueError("at least one feature-family report is required")

    baseline_reference: list[str] | None = None
    components: list[dict[str, Any]] = []
    selected_groups: list[str] = []

    for name in sorted(component_reports):
        report = component_reports[name]
        baseline = _baseline_features(report)
        if baseline_reference is None:
            baseline_reference = baseline
        elif baseline != baseline_reference:
            raise ValueError("feature-family reports do not share the same baseline")

        feature_names = _feature_names(report)
        component = {
            "name": name,
            "feature_names": feature_names,
            "feature_count": len(feature_names),
            "report_sha256": _sha256_text(_canonical_json(report)),
            **_summary_metrics(report),
            "walk_forward_samples": sum(
                int(item.get("walk_forward_samples", 0))
                for item in (report.get("by_horizon") or {}).values()
                if isinstance(item, dict)
            ),
        }
        components.append(component)
        if (
            component["recommendation"] == "edge_detected"
            and component["no_material_horizon_regression"] is True
            and int(component["statistically_better_horizons"] or 0) > 0
        ):
            selected_groups.append(name)

    baseline_reference = baseline_reference or []
    selected_features = sorted(
        {
            feature
            for component in components
            if component["name"] in selected_groups
            for feature in component["feature_names"]
        }
    )
    version_payload = {
        "version": SELECTION_VERSION,
        "baseline_features": baseline_reference,
        "selected_groups": selected_groups,
        "components": [
            {
                "name": component["name"],
                "report_sha256": component["report_sha256"],
                "recommendation": component["recommendation"],
            }
            for component in components
        ],
    }
    payload_hash = _sha256_text(_canonical_json(version_payload))

    return {
        "schema_version": SELECTION_VERSION,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "baseline_features": baseline_reference,
        "feature_set_version": f"feature-set-{payload_hash[:16]}",
        "component_count": len(components),
        "components": components,
        "selection": {
            "selected_groups": selected_groups,
            "selected_feature_names": selected_features,
            "versioning": {"method": "sha256", "payload_sha256": payload_hash},
        },
    }


def render_summary(report: Mapping[str, Any]) -> str:
    lines = [
        "# Feature selection pipeline",
        "",
        f"- Feature-set version: **{report['feature_set_version']}**",
        f"- Selected groups: **{', '.join(report['selection']['selected_groups']) or 'none'}**",
        "",
        "| Group | Samples | Mean improvement | Better horizons | Recommendation |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for component in report["components"]:
        lines.append(
            f"| {component['name']} | {component['walk_forward_samples']} | "
            f"{component['mean_relative_mae_improvement'] if component['mean_relative_mae_improvement'] is not None else '--'} | "
            f"{component['statistically_better_horizons'] if component['statistically_better_horizons'] is not None else '--'} | "
            f"{component['recommendation']} |"
        )
    lines.extend(
        [
            "",
            "The selected feature set is versioned so experiment manifests and follow-up runs can reproduce it exactly.",
            "",
        ]
    )
    return "\n".join(lines)


def load_feature_family_reports(paths: Mapping[str, Path]) -> dict[str, dict[str, Any]]:
    return {name: _read_json(path) for name, path in paths.items() if path.exists()}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the feature-family selection report")
    parser.add_argument(
        "--derivatives-report", type=Path, default=DEFAULT_REPORT_PATHS["derivatives"]
    )
    parser.add_argument(
        "--microstructure-report", type=Path, default=DEFAULT_REPORT_PATHS["microstructure"]
    )
    parser.add_argument(
        "--cross-asset-report", type=Path, default=DEFAULT_REPORT_PATHS["cross_asset"]
    )
    args = parser.parse_args()

    reports = load_feature_family_reports(
        {
            "derivatives": args.derivatives_report,
            "microstructure": args.microstructure_report,
            "cross_asset": args.cross_asset_report,
        }
    )
    report = build_feature_selection_report(reports)
    REPORT_PATH.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    SUMMARY_PATH.write_text(render_summary(report), encoding="utf-8")
    print(SUMMARY_PATH.read_text(encoding="utf-8"))
    print(f"Saved {REPORT_PATH} and {SUMMARY_PATH}")


if __name__ == "__main__":
    main()
