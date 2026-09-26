"""Fail-closed contract gate for frozen champion/challenger confirmation (#350).

This module validates evidence structure only. It never scores candidates or selects a
winner; a structurally complete contract is merely ready for a separately frozen
evaluation, and all operational decisions remain human-reviewed.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

HORIZONS = (2, 4, 8, 16)
IDENTITY_FIELDS = ("source_id", "vintage_id", "data_id", "model_id", "policy_id")
REQUIRED_METHOD = "moving_block"


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
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, Mapping):
            reasons.append(f"candidate[{index}] is not a manifest")
            continue
        candidate_id = str(candidate.get("candidate_id", ""))
        if not candidate_id or candidate_id in ids:
            reasons.append(f"candidate[{index}] has missing or duplicate candidate_id")
        ids.add(candidate_id)
        attempted += 1
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
            key = (
                str(row.get("origin", "")),
                str(row.get("target", "")),
                int(row.get("horizon", 0) or 0),
            )
            keys.append(key)
            if not row.get("raw") or not row.get("final") or "failure" not in row:
                reasons.append(f"candidate {candidate_id} dropped raw/final forecast tracking")
            if row.get("failure"):
                reasons.append(f"candidate {candidate_id} has failed forecasts in eligible pairs")
            label_time = _time(row.get("label_available_at"), "label_available_at", reasons)
            if label_time and cutoff and label_time > cutoff:
                reasons.append(f"candidate {candidate_id} selection label is after cutoff")
        if len(keys) != len(set(keys)):
            reasons.append(f"candidate {candidate_id} has duplicate origin/target/horizon pairs")
        frozen_pairs = freeze.get("selection_pairs")
        if isinstance(frozen_pairs, list) and sorted(keys) != sorted(
            (
                str(row.get("origin", "")),
                str(row.get("target", "")),
                int(row.get("horizon", 0) or 0),
            )
            for row in frozen_pairs
            if isinstance(row, Mapping)
        ):
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
    if selection.get("candidate_family_count") != attempted:
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
    if frozen_at and prospect_start and prospect_start < frozen_at:
        reasons.append("prospective D3 must start after the freeze")
    if (
        prospect_start
        and prospect_end
        and (prospect_end - prospect_start).total_seconds() < 30 * 86400
    ):
        reasons.append("prospective D3 must span at least 30 calendar days")
    counts = prospective.get("exact_mature_pairs_by_horizon")
    if not isinstance(counts, Mapping):
        reasons.append("prospective exact mature-pair counts missing")
    else:
        for horizon in HORIZONS:
            if int(counts.get(str(horizon), counts.get(horizon, 0)) or 0) < 200:
                reasons.append(
                    f"prospective D3 requires at least 200 exact mature pairs at {horizon}h"
                )
    if prospective.get("fixed_cutoff") is not True or prospective.get(
        "stopping_rule"
    ) != freeze.get("stopping_rule"):
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
