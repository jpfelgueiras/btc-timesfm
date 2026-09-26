"""Fail-closed contract gate for frozen champion/challenger confirmation (#350).

This module validates evidence structure only. It never scores candidates or selects a
winner; a structurally complete contract is merely ready for a separately frozen
evaluation, and all operational decisions remain human-reviewed.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

HORIZONS = (2, 4, 8, 16)
IDENTITY_FIELDS = ("source_id", "vintage_id", "data_id", "model_id", "policy_id")
REQUIRED_METHOD = "moving_block"


def _positive_int(value: object) -> int | None:
    """Accept only an exact positive JSON integer (excluding bool and coercions)."""
    if type(value) is not int or value <= 0:
        return None
    return value


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def _forecast_record(value: object) -> bool:
    return isinstance(value, Mapping) and _number(value.get("prediction")) is not None


def _time(value: object, label: str, reasons: list[str]) -> datetime | None:
    if not isinstance(value, str):
        reasons.append(f"{label} must be an ISO-8601 timestamp with timezone")
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        reasons.append(f"{label} must be an ISO-8601 timestamp with timezone")
        return None
    return parsed


def _utc_time(value: object, label: str, reasons: list[str]) -> datetime | None:
    parsed = _time(value, label, reasons)
    if parsed is not None and parsed.utcoffset() != timedelta(0):
        reasons.append(f"{label} must use UTC")
        return None
    return parsed


def _pair_key(
    row: Mapping[str, Any], label: str, reasons: list[str]
) -> tuple[str, str, int] | None:
    horizon = _positive_int(row.get("horizon"))
    if horizon is None:
        reasons.append(f"{label} horizon must be an exact positive integer")
        return None
    origin = _utc_time(row.get("origin"), f"{label} origin", reasons)
    target = _utc_time(row.get("target"), f"{label} target", reasons)
    if origin is None or target is None:
        return None
    if target - origin != timedelta(hours=horizon):
        reasons.append(f"{label} target must equal origin plus horizon")
    return (origin.isoformat(), target.isoformat(), horizon)


def validate_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Validate frozen selection and untouched prospective confirmation evidence."""
    reasons: list[str] = []
    freeze = contract.get("freeze")
    candidates = contract.get("candidates")
    selection = contract.get("selection")
    prospective = contract.get("prospective")
    if not isinstance(freeze, Mapping):
        reasons.append("freeze manifest missing")
        freeze = {}
    if not isinstance(candidates, list) or not candidates:
        reasons.append("candidate registry missing; every attempted candidate must be included")
        candidates = []
    if not isinstance(selection, Mapping):
        reasons.append("selection evidence missing")
        selection = {}
    if not isinstance(prospective, Mapping):
        reasons.append("prospective D3 evidence missing")
        prospective = {}

    for key in ("frozen_at", "cutoff_at", "stopping_rule", "corpus_sha256", "family_id"):
        if not freeze.get(key):
            reasons.append(f"freeze manifest missing {key}")
    frozen_at = _time(freeze.get("frozen_at"), "freeze.frozen_at", reasons)
    cutoff = _time(freeze.get("cutoff_at"), "freeze.cutoff_at", reasons)
    frozen_evaluation_cutoff = _time(
        freeze.get("evaluation_cutoff"), "freeze.evaluation_cutoff", reasons
    )
    if frozen_at and cutoff and cutoff > frozen_at:
        reasons.append("selection cutoff must not be after freeze time")
    if freeze.get("stopping_rule") and "fixed" not in str(freeze["stopping_rule"]).lower():
        reasons.append("stopping rule must be fixed before evaluation")

    expected_ids = {key: freeze.get(key) for key in IDENTITY_FIELDS}
    for key, value in expected_ids.items():
        if not value:
            reasons.append(f"freeze manifest missing identity {key}")

    ids: set[str] = set()
    eligible: list[Mapping[str, Any]] = []
    attempted = 0
    common_origins: tuple[tuple[str, str, int], ...] | None = None
    frozen_pairs = freeze.get("selection_pairs")
    frozen_keys: list[tuple[str, str, int]] = []
    if not isinstance(frozen_pairs, list) or not frozen_pairs:
        reasons.append("freeze manifest requires a nonempty frozen selection_pairs set")
    else:
        for row in frozen_pairs:
            if not isinstance(row, Mapping):
                reasons.append("frozen selection_pairs contains an invalid row")
                continue
            key = _pair_key(row, "frozen selection pair", reasons)
            if key is not None:
                frozen_keys.append(key)
        if len(frozen_keys) != len(set(frozen_keys)):
            reasons.append("frozen selection_pairs contains duplicate origin/target/horizon keys")
    for index, candidate in enumerate(candidates):
        attempted += 1
        if not isinstance(candidate, Mapping):
            reasons.append(f"candidate[{index}] is not a manifest")
            continue
        candidate_id = str(candidate.get("candidate_id", ""))
        if not candidate_id or candidate_id in ids:
            reasons.append(f"candidate[{index}] has missing or duplicate candidate_id")
        ids.add(candidate_id)
        status = candidate.get("status")
        if status not in {"eligible", "blocked", "failed", "ineligible"}:
            reasons.append(f"candidate {candidate_id or index} has invalid status")
        for field, expected in expected_ids.items():
            if status == "eligible" and expected and candidate.get(field) != expected:
                reasons.append(f"candidate {candidate_id or index} identity mismatch: {field}")
        if status != "eligible":
            continue
        eligible.append(candidate)
        origins = candidate.get("pairs")
        if not isinstance(origins, list) or not origins:
            reasons.append(f"candidate {candidate_id} has no complete paired origins")
            continue
        keys: list[tuple[str, str, int]] = []
        for row in origins:
            if not isinstance(row, Mapping):
                reasons.append(f"candidate {candidate_id} contains invalid pair row")
                continue
            key = _pair_key(row, f"candidate {candidate_id} selection pair", reasons)
            if key is None:
                continue
            keys.append(key)
            if (
                not _forecast_record(row.get("raw"))
                or not _forecast_record(row.get("final"))
                or "failure" not in row
            ):
                reasons.append(f"candidate {candidate_id} dropped raw/final forecast tracking")
            if row.get("failure"):
                reasons.append(f"candidate {candidate_id} has failed forecasts in eligible pairs")
            label_time = _time(row.get("label_available_at"), "label_available_at", reasons)
            if label_time and cutoff and label_time > cutoff:
                reasons.append(f"candidate {candidate_id} selection label is after cutoff")
        if len(keys) != len(set(keys)):
            reasons.append(f"candidate {candidate_id} has duplicate origin/target/horizon pairs")
        if frozen_keys and sorted(keys) != sorted(frozen_keys):
            reasons.append(
                f"candidate {candidate_id} does not match frozen selection origin/target set"
            )
        if common_origins is None:
            common_origins = tuple(sorted(keys))
        elif tuple(sorted(keys)) != common_origins:
            reasons.append(
                f"candidate {candidate_id} does not share the exact paired origin/target set"
            )
        present = {key[2] for key in keys}
        if present != set(HORIZONS):
            reasons.append(f"candidate {candidate_id} must contain all four horizons {HORIZONS}")
        for horizon in HORIZONS:
            count = sum(key[2] == horizon for key in keys)
            if count < 1:
                reasons.append(f"candidate {candidate_id} has no exact pairs for {horizon}h")

    if not eligible:
        reasons.append("no eligible complete challenger is registered")
    family_count = _positive_int(selection.get("candidate_family_count"))
    if family_count is None:
        reasons.append("candidate_family_count must be an exact positive integer")
    if family_count != attempted:
        reasons.append(
            "multiple-test family count must include every attempted candidate, including failures"
        )
    if selection.get("adjustment") not in {"holm", "bonferroni", "sidak", "by"}:
        reasons.append("selection requires a declared whole-family multiple-test adjustment")
    if selection.get("dependence_method") != REQUIRED_METHOD:
        reasons.append("selection requires dependence-aware moving-block evidence")
    required_baselines = {"champion", "persistence", "strongest_naive"}
    if not required_baselines.issubset(set(selection.get("baselines", []))):
        reasons.append(
            "selection must compare champion, persistence, and strongest naive baselines"
        )
    if selection.get("nested_folds") is not True:
        reasons.append("selection must use nested earlier folds")
    if selection.get("final_holdout_reused") is not False:
        reasons.append("final holdout must be disjoint and never reused for selection")

    prospect_start = _time(prospective.get("start_at"), "prospective.start_at", reasons)
    prospect_end = _time(prospective.get("end_at"), "prospective.end_at", reasons)
    evaluation_cutoff = _time(
        prospective.get("evaluation_cutoff"), "prospective.evaluation_cutoff", reasons
    )
    if frozen_evaluation_cutoff is None or evaluation_cutoff != frozen_evaluation_cutoff:
        reasons.append(
            "prospective evaluation_cutoff must exactly match the frozen manifest cutoff"
        )
    if frozen_at and prospect_start and prospect_start <= frozen_at:
        reasons.append("prospective D3 must start after the freeze")
    if (
        prospect_start
        and prospect_end
        and (prospect_end - prospect_start).total_seconds() < 30 * 86400
    ):
        reasons.append("prospective D3 must span at least 30 calendar days")
    if prospect_start and prospect_end and prospect_end <= prospect_start:
        reasons.append("prospective end_at must be after start_at")
    if evaluation_cutoff and prospect_end and evaluation_cutoff > prospect_end:
        reasons.append("prospective evaluation_cutoff must not be after end_at")
    if evaluation_cutoff and prospect_start and evaluation_cutoff <= prospect_start:
        reasons.append("prospective evaluation_cutoff must be inside the prospective interval")
    prospective_pairs = prospective.get("pairs")
    if not isinstance(prospective_pairs, Mapping):
        reasons.append("prospective exact pair records by candidate are missing")
        prospective_pairs = {}
    for candidate in eligible:
        candidate_id = str(candidate.get("candidate_id"))
        rows = prospective_pairs.get(candidate_id)
        if not isinstance(rows, list):
            reasons.append(f"candidate {candidate_id} prospective exact pair records missing")
            continue
        counts = dict.fromkeys(HORIZONS, 0)
        seen: set[tuple[str, str, int]] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                reasons.append(f"candidate {candidate_id} prospective pair is invalid")
                continue
            pair_key = _pair_key(row, f"candidate {candidate_id} prospective pair", reasons)
            if pair_key is None:
                continue
            origin = _utc_time(row.get("origin"), "prospective pair origin", reasons)
            target = _utc_time(row.get("target"), "prospective pair target", reasons)
            horizon = pair_key[2]
            if horizon not in HORIZONS:
                reasons.append(
                    f"candidate {candidate_id} prospective horizon {horizon} is unsupported"
                )
                continue
            actual_at = _utc_time(row.get("actual_at"), "prospective pair actual_at", reasons)
            matured_at = _utc_time(row.get("matured_at"), "prospective pair matured_at", reasons)
            actual_value = _number(row.get("actual_value"))
            row_failure = row.get("failure")
            identity_ok = True
            for field in ("source_id", "vintage_id"):
                if row.get(field) != expected_ids.get(field):
                    identity_ok = False
                    reasons.append(
                        f"candidate {candidate_id} prospective pair identity mismatch: {field}"
                    )
            has_stages = (
                _forecast_record(row.get("raw"))
                and _forecast_record(row.get("final"))
                and "failure" in row
            )
            if not has_stages:
                reasons.append(
                    f"candidate {candidate_id} prospective row missing raw/final/failure tracking"
                )
            if row_failure:
                reasons.append(f"candidate {candidate_id} has failed prospective forecast rows")
            if actual_value is None:
                reasons.append(
                    f"candidate {candidate_id} prospective actual_value must be finite numeric"
                )
            if origin and target:
                key = pair_key
                if key in seen:
                    reasons.append(
                        f"candidate {candidate_id} has duplicate prospective exact pairs"
                    )
                seen.add(key)
                if prospect_start and origin <= prospect_start:
                    reasons.append(
                        f"candidate {candidate_id} prospective origin is not after D3 start"
                    )
                if prospect_end and target > prospect_end:
                    reasons.append(f"candidate {candidate_id} prospective target is after D3 end")
                if actual_at and actual_at != target:
                    reasons.append(f"candidate {candidate_id} actual_at must exactly equal target")
            if matured_at and target and matured_at < target:
                reasons.append(f"candidate {candidate_id} outcome matured before its target")
            if matured_at and evaluation_cutoff and matured_at > evaluation_cutoff:
                reasons.append(f"candidate {candidate_id} outcome matured after evaluation cutoff")
            if actual_at and evaluation_cutoff and actual_at >= evaluation_cutoff:
                reasons.append(f"candidate {candidate_id} actual_at must precede evaluation cutoff")
            if matured_at and evaluation_cutoff and matured_at >= evaluation_cutoff:
                reasons.append(f"candidate {candidate_id} maturity must precede evaluation cutoff")
            if (
                origin
                and target
                and actual_at == target
                and actual_value is not None
                and matured_at
                and evaluation_cutoff
                and identity_ok
                and has_stages
                and not row_failure
                and matured_at <= evaluation_cutoff
                and actual_at < evaluation_cutoff
            ):
                counts[horizon] += 1
        for horizon, count in counts.items():
            if count < 200:
                reasons.append(
                    f"candidate {candidate_id} prospective D3 has fewer than 200 exact mature pairs at {horizon}h"
                )
        _validate_acceptance(candidate, candidate_id, reasons)
    if (
        prospective.get("fixed_cutoff") is not True
        or not evaluation_cutoff
        or prospective.get("stopping_rule") != freeze.get("stopping_rule")
    ):
        reasons.append("prospective confirmation must use the frozen cutoff and stopping rule")
    if prospective.get("final_holdout_disjoint") is not True:
        reasons.append("prospective D3 must be disjoint from selection and final holdout")

    return {
        "status": "blocked" if reasons else "ready_for_evaluation",
        "reasons": list(dict.fromkeys(reasons)),
        "attempted_candidate_count": attempted,
        "eligible_candidate_ids": [str(item.get("candidate_id")) for item in eligible],
        "candidate_winner": None,
        "human_recommendation_only": True,
        "automatic_promotion": False,
    }


def _validate_acceptance(
    candidate: Mapping[str, Any], candidate_id: str, reasons: list[str]
) -> None:
    evidence = candidate.get("acceptance")
    if not isinstance(evidence, Mapping) or evidence.get("preregistered") is not True:
        reasons.append(f"candidate {candidate_id} acceptance criteria must be preregistered")
        return
    improvement = _number(evidence.get("adjusted_improvement_pct"))
    ci_lower = _number(evidence.get("adjusted_ci_lower_pct"))
    if improvement is None or improvement < 3:
        reasons.append(f"candidate {candidate_id} adjusted primary improvement must be at least 3%")
    if ci_lower is None or ci_lower <= 0:
        reasons.append(
            f"candidate {candidate_id} adjusted confidence interval lower bound must exceed 0"
        )
    skills = evidence.get("positive_skill_vs")
    if not isinstance(skills, Mapping) or any(
        _number(skills.get(name)) is None or _number(skills.get(name)) <= 0
        for name in ("persistence", "strongest_naive")
    ):
        reasons.append(
            f"candidate {candidate_id} must show positive skill vs persistence and strongest naive"
        )
    regressions = evidence.get("powered_regression_pct")
    if not isinstance(regressions, list) or not regressions:
        reasons.append(f"candidate {candidate_id} powered horizon/segment regressions missing")
    elif any(_number(value) is None or _number(value) > 5 for value in regressions):
        reasons.append(f"candidate {candidate_id} powered regression exceeds 5% or is invalid")
    directional_loss = _number(evidence.get("directional_loss_pp"))
    if directional_loss is None or directional_loss > 2:
        reasons.append(
            f"candidate {candidate_id} directional loss must not exceed 2 percentage points"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.contract:
        raw = json.loads(args.contract.read_text(encoding="utf-8"))
        report = validate_contract(raw if isinstance(raw, Mapping) else {})
    else:
        report = validate_contract({})
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
