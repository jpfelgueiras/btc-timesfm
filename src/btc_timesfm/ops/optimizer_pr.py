#!/usr/bin/env python3
"""Open a reviewable PR for a validated optimizer candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


DEFAULT_OPTIMIZER_REPORT = Path("optimizer_report.json")
DEFAULT_PROMOTION_DECISION = Path("promotion_decision.json")
DEFAULT_CHAMPION_REPORT = Path("champion_challenger_report.json")
DEFAULT_BASE_BRANCH = "main"
BRANCH_PREFIX = "optimizer-pr"
PR_LABEL = "optimizer-promotion"


@dataclass(frozen=True)
class FileEdit:
    path: Path
    before: str
    after: str


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _format_number(value: object) -> str:
    if isinstance(value, bool):
        raise ValueError("boolean values are not valid optimizer parameters")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return text if "." in text else f"{text}.0"
    raise ValueError(f"unsupported numeric parameter: {value!r}")


def _replace_assignment(text: str, name: str, replacement: str) -> str:
    pattern = re.compile(rf"(?m)^({re.escape(name)}\s*=\s*)(.+)$")
    if not pattern.search(text):
        raise ValueError(f"could not find assignment for {name}")
    return pattern.sub(lambda match: f"{match.group(1)}{replacement}", text, count=1)


def _replace_env_default(text: str, env_name: str, replacement: str) -> str:
    pattern = re.compile(rf'("{re.escape(env_name)}",\s*")([^"]+)(")')
    if not pattern.search(text):
        raise ValueError(f"could not find environment default for {env_name}")
    return pattern.sub(
        lambda match: f"{match.group(1)}{replacement}{match.group(3)}", text, count=1
    )


def build_candidate_file_edits(repo_root: Path, candidate: Mapping[str, Any]) -> list[FileEdit]:
    parameters = candidate.get("parameters")
    if not isinstance(parameters, Mapping):
        raise ValueError("candidate parameters are missing")

    required = {
        "min_samples",
        "full_samples",
        "max_blend",
        "min_weight",
        "max_weight",
        "mae_lambda",
        "direction_reward",
        "persistence_boost",
        "history_limit",
        "target_interval_coverage",
        "coverage_penalty",
    }
    missing = sorted(required - set(parameters))
    if missing:
        raise ValueError(f"candidate is missing parameters: {', '.join(missing)}")

    forecast_path = repo_root / "src/btc_timesfm/forecasting/forecast_engine.py"
    adaptive_path = repo_root / "src/btc_timesfm/forecasting/adaptive_weighting.py"
    forecast_before = forecast_path.read_text(encoding="utf-8")
    adaptive_before = adaptive_path.read_text(encoding="utf-8")

    forecast_after = forecast_before
    parameter_names = {
        "ADAPTIVE_MIN_SAMPLES": "min_samples",
        "ADAPTIVE_FULL_SAMPLES": "full_samples",
        "ADAPTIVE_MAX_BLEND": "max_blend",
        "ADAPTIVE_MIN_WEIGHT": "min_weight",
        "ADAPTIVE_MAX_WEIGHT": "max_weight",
        "ADAPTIVE_MAE_LAMBDA": "mae_lambda",
        "ADAPTIVE_DIRECTION_REWARD": "direction_reward",
        "PERSISTENCE_FALLBACK_BOOST": "persistence_boost",
    }
    for name in (
        "ADAPTIVE_MIN_SAMPLES",
        "ADAPTIVE_FULL_SAMPLES",
        "ADAPTIVE_MAX_BLEND",
        "ADAPTIVE_MIN_WEIGHT",
        "ADAPTIVE_MAX_WEIGHT",
        "ADAPTIVE_MAE_LAMBDA",
        "ADAPTIVE_DIRECTION_REWARD",
        "PERSISTENCE_FALLBACK_BOOST",
    ):
        key = parameter_names[name]
        forecast_after = _replace_assignment(forecast_after, name, _format_number(parameters[key]))

    adaptive_after = adaptive_before
    adaptive_after = _replace_env_default(
        adaptive_after,
        "BTC_ADAPTIVE_HISTORY_LIMIT",
        _format_number(parameters["history_limit"]),
    )
    adaptive_after = _replace_assignment(
        adaptive_after,
        "TARGET_INTERVAL_COVERAGE",
        _format_number(parameters["target_interval_coverage"]),
    )
    adaptive_after = _replace_assignment(
        adaptive_after,
        "COVERAGE_PENALTY",
        _format_number(parameters["coverage_penalty"]),
    )

    edits = [
        FileEdit(forecast_path, forecast_before, forecast_after),
        FileEdit(adaptive_path, adaptive_before, adaptive_after),
    ]
    return [edit for edit in edits if edit.before != edit.after]


def _branch_name(candidate: Mapping[str, Any], champion_report: Mapping[str, Any]) -> str:
    comparison_id = str(champion_report.get("comparison_id", "comparison"))
    comparison_hash = _sha256_text(
        _canonical_json({"comparison_id": comparison_id, "candidate": candidate})
    )[:10]
    candidate_name = re.sub(
        r"[^a-zA-Z0-9._-]+", "-", str(candidate.get("name", "candidate"))
    ).strip("-")
    return f"{BRANCH_PREFIX}/{candidate_name}-{comparison_hash}"


def build_pr_body(
    optimizer_report: Mapping[str, Any],
    promotion_decision: Mapping[str, Any],
    champion_report: Mapping[str, Any],
) -> str:
    candidate = promotion_decision.get("candidate", {})
    evidence = promotion_decision.get("evidence", {})
    policy = promotion_decision.get("policy_id", "unknown")

    lines = [
        "# Safe optimizer parameter change",
        "",
        f"- Policy: `{policy}`",
        f"- Candidate: **{candidate.get('name', 'unknown')}**",
        f"- Comparison: `{champion_report.get('comparison_id', 'unknown')}`",
        f"- Optimizer schema: `{optimizer_report.get('schema_version', 'unknown')}`",
        "",
        "## Evidence",
        "",
        f"- Samples: {candidate.get('samples', 'n/a')}",
        f"- Relative MAE improvement: {float(evidence.get('relative_mae_improvement', 0.0)) * 100:.2f}%",
    ]

    for horizon, value in sorted((evidence.get("horizon_relative_improvement") or {}).items()):
        lines.append(f"- {horizon}: {float(value) * 100:.2f}%")

    lines.extend(
        [
            "",
            "## Manifests",
            "",
            f"- Champion manifest: `{champion_report.get('champion', {}).get('manifest', {}).get('configuration_id', 'n/a')}`",
            f"- Challenger manifest: `{champion_report.get('challenger', {}).get('manifest', {}).get('configuration_id', 'n/a')}`",
            "",
            "## Decision checks",
            "",
        ]
    )

    checks = promotion_decision.get("checks", {})
    if isinstance(checks, Mapping):
        for section in ("hard_veto", "review_requirements"):
            for name, passed in (checks.get(section, {}) or {}).items():
                lines.append(f"- {'✅' if passed else '❌'} `{section}:{name}`")

    lines.extend(
        [
            "",
            "Only the explicit optimizer-owned configuration fields are changed in this PR.",
            "Repeated runs reuse the same evidence-derived branch name, so the automation updates an existing open PR instead of spamming duplicates.",
            "",
        ]
    )
    return "\n".join(lines)


def _run(command: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def prepare_pull_request(
    repo_root: Path,
    optimizer_report: Mapping[str, Any],
    promotion_decision: Mapping[str, Any],
    champion_report: Mapping[str, Any],
    *,
    base_branch: str = DEFAULT_BASE_BRANCH,
    dry_run: bool = False,
) -> dict[str, Any]:
    candidate = promotion_decision.get("candidate", {})
    if promotion_decision.get("decision") != "review":
        raise ValueError("promotion decision must be review-approved before opening a PR")
    challenger_name = (
        champion_report.get("challenger", {}).get("manifest", {}).get("name")
        if isinstance(champion_report, Mapping)
        else None
    )
    if challenger_name and candidate.get("name") != challenger_name:
        raise ValueError(
            "promotion decision candidate does not match the champion-challenger report"
        )
    branch = _branch_name(candidate, champion_report)
    edits = build_candidate_file_edits(repo_root, candidate)
    body = build_pr_body(optimizer_report, promotion_decision, champion_report)
    title = f"optimizer: promote {candidate.get('name', 'candidate')}"

    if dry_run:
        return {
            "branch": branch,
            "title": title,
            "body": body,
            "edits": [str(edit.path.relative_to(repo_root)) for edit in edits],
        }

    _run(["git", "checkout", "-B", branch, base_branch], cwd=repo_root)
    for edit in edits:
        edit.path.write_text(edit.after, encoding="utf-8")

    status = _run(["git", "status", "--porcelain"], cwd=repo_root)
    if status.strip():
        _run(
            [
                "git",
                "add",
                "src/btc_timesfm/forecasting/forecast_engine.py",
                "src/btc_timesfm/forecasting/adaptive_weighting.py",
            ],
            cwd=repo_root,
        )
        _run(["git", "commit", "-m", title], cwd=repo_root)

    existing = _run(
        ["gh", "pr", "list", "--state", "open", "--head", branch, "--json", "number,url"],
        cwd=repo_root,
    )
    open_prs = json.loads(existing or "[]")
    if open_prs:
        pr_number = open_prs[0]["number"]
        _run(["gh", "pr", "edit", str(pr_number), "--title", title, "--body", body], cwd=repo_root)
        return {"branch": branch, "title": title, "body": body, "pr_number": pr_number}

    _run(
        [
            "gh",
            "pr",
            "create",
            "--base",
            base_branch,
            "--head",
            branch,
            "--title",
            title,
            "--body",
            body,
            "--label",
            PR_LABEL,
        ],
        cwd=repo_root,
    )
    return {"branch": branch, "title": title, "body": body}


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare a reviewable optimizer PR")
    parser.add_argument("--optimizer-report", type=Path, default=DEFAULT_OPTIMIZER_REPORT)
    parser.add_argument("--promotion-decision", type=Path, default=DEFAULT_PROMOTION_DECISION)
    parser.add_argument("--champion-report", type=Path, default=DEFAULT_CHAMPION_REPORT)
    parser.add_argument("--base-branch", type=str, default=DEFAULT_BASE_BRANCH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repo_root = Path(_run(["git", "rev-parse", "--show-toplevel"], cwd=Path.cwd()))
    optimizer_report = _read_json(args.optimizer_report)
    promotion_decision = _read_json(args.promotion_decision)
    champion_report = _read_json(args.champion_report)
    result = prepare_pull_request(
        repo_root,
        optimizer_report,
        promotion_decision,
        champion_report,
        base_branch=args.base_branch,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
