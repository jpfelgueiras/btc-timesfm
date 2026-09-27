"""Blocked-first bounded candidate/confirmation outcome for roadmap v10 issue #400."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 1
DEFAULT_PARITY = Path("docs/research/ISSUE_399_PRODUCTION_PARITY_BLOCKED.json")
DEFAULT_OUTPUT = Path("docs/research/ISSUE_400_CANDIDATE_CONFIRMATION_BLOCKED.json")

PROTOCOL = {
    "hypotheses": "Freeze directional, bounded candidate-versus-champion hypotheses before scoring.",
    "candidate_family": "Hash an immutable preregistration manifest that enumerates every candidate, its configuration/identity, the bounded family, and all planned comparisons before scoring; record every attempted, failed, blocked, and excluded candidate.",
    "outcomes": ["MAE", "signed_bias", "direction_accuracy", "failures"],
    "horizons_hours": [2, 4, 8, 16],
    "folds": "Nested earlier purged walk-forward folds; all candidates, frozen champion, persistence, and strongest naive baseline use the exact same origin/target/horizon keys.",
    "baselines": ["frozen_champion", "persistence", "strongest_naive"],
    "minimum_effect": "Preregister a primary improvement floor of at least 3%.",
    "stopping_rule": "Fixed cutoff and stopping rule declared before evaluation; no optional stopping.",
    "thresholds": "Freeze primary metric, adjusted confidence lower bound greater than zero, positive skill versus persistence and strongest naive baseline, per-horizon/segment powered regression no greater than 5%, and directional-loss increase no greater than 2 percentage points before scoring.",
    "uncertainty": "Paired dependence-aware moving-block uncertainty using the existing final-selection contract.",
    "multiplicity": "Predeclare a whole-family adjustment (Holm, Bonferroni, Sidak, or BY as appropriate) across every attempted candidate, comparison, horizon, and reported stratum; retain failed attempts in family accounting.",
    "prospective_confirmation": "After selection freeze, confirm only the selected candidate on disjoint prospective D3 shadow evidence; minimum 30 calendar days and 200 exact mature pairs at each supported horizon (2h, 4h, 8h, 16h). Apply the frozen per-horizon regression and directional-loss safeguards as well as primary, adjusted-interval, and baseline-skill criteria; track every attempt and failure.",
    "review": "Selection is recommendation-only and requires human review; no automatic winner, promotion, or production forecast/configuration changes.",
}


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )


def protocol_sha256() -> str:
    return hashlib.sha256(_canonical_json(PROTOCOL)).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_completed_evidence(
    outcome: Mapping[str, Any], status: str, reasons: list[str]
) -> tuple[set[str], set[str]]:
    manifest = outcome.get("preregistration_manifest")
    planned_ids: set[str] = set()
    planned_candidates: set[str] = set()
    manifest_valid = isinstance(manifest, Mapping)
    planned_pairs: set[tuple[str, str]] = set()
    if not manifest_valid:
        reasons.append(f"{status} outcome requires a preregistration manifest mapping")
    else:
        if manifest.get("protocol_sha256") != protocol_sha256():
            reasons.append(
                "preregistration manifest protocol_sha256 does not match current protocol"
            )
        candidates = manifest.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            reasons.append("preregistration manifest requires a nonempty candidate list")
        else:
            candidate_ids: list[str] = []
            for candidate in candidates:
                candidate_id = (
                    candidate.get("candidate_id") if isinstance(candidate, Mapping) else None
                )
                if not isinstance(candidate_id, str) or not candidate_id.strip():
                    reasons.append("manifest candidates require nonempty candidate_id values")
                    continue
                candidate_ids.append(candidate_id)
            if len(candidate_ids) != len(set(candidate_ids)):
                reasons.append("manifest candidate IDs must be unique")
            planned_candidates = set(candidate_ids)

        planned = manifest.get("planned_attempts")
        planned_attempt_keys: list[tuple[str, str]] = []
        if not isinstance(planned, list) or not planned:
            reasons.append("preregistration manifest requires a nonempty planned_attempts list")
        else:
            for attempt in planned:
                if not isinstance(attempt, Mapping):
                    reasons.append("planned attempts must be mappings")
                    continue
                attempt_id, candidate_id = attempt.get("attempt_id"), attempt.get("candidate_id")
                if not isinstance(attempt_id, str) or not attempt_id.strip():
                    reasons.append("planned attempts require nonempty attempt_id values")
                    continue
                if not isinstance(candidate_id, str) or not candidate_id.strip():
                    reasons.append("planned attempts require nonempty candidate_id values")
                    continue
                if candidate_id not in planned_candidates:
                    reasons.append("planned attempt candidate_id is absent from candidate registry")
                planned_attempt_keys.append((attempt_id, candidate_id))
            planned_ids = {attempt_id for attempt_id, _ in planned_attempt_keys}
            if len(planned_ids) != len(planned_attempt_keys):
                reasons.append("planned attempt IDs must be unique")
            planned_attempt_candidates = [candidate_id for _, candidate_id in planned_attempt_keys]
            if len(set(planned_attempt_candidates)) != len(planned_attempt_candidates):
                reasons.append("planned attempt candidate IDs must be unique")
            planned_pairs = set(planned_attempt_keys)
            if set(planned_attempt_candidates) != planned_candidates:
                reasons.append("planned attempts must cover exactly the frozen candidate registry")

        if manifest_valid:
            try:
                expected_hash = hashlib.sha256(_canonical_json(manifest)).hexdigest()
            except (TypeError, ValueError, OverflowError, RecursionError):
                expected_hash = None
            if not _is_sha256(outcome.get("preregistration_sha256")) or (
                outcome.get("preregistration_sha256") != expected_hash
            ):
                reasons.append("preregistration_sha256 does not match canonical manifest bytes")

    attempts = outcome.get("attempts")
    observed_ids: list[str] = []
    observed_candidates: list[str] = []
    scored_candidates: set[str] = set()
    attempts_valid = isinstance(attempts, list) and bool(attempts)
    if outcome.get("attempts_complete") is not True or not attempts_valid:
        reasons.append(f"{status} outcome requires nonempty complete attempt/failure accounting")
    elif attempts_valid:
        for attempt in attempts:
            if not isinstance(attempt, Mapping):
                reasons.append("observed attempts must be mappings")
                continue
            attempt_id = attempt.get("attempt_id")
            candidate_id = attempt.get("candidate_id")
            disposition = attempt.get("disposition")
            if not isinstance(attempt_id, str) or not attempt_id.strip():
                reasons.append("observed attempts require nonempty attempt_id values")
            else:
                observed_ids.append(attempt_id)
            if not isinstance(candidate_id, str) or not candidate_id.strip():
                reasons.append("observed attempts require nonempty candidate_id values")
            else:
                observed_candidates.append(candidate_id)
            if not isinstance(disposition, str) or disposition not in {
                "scored",
                "failed",
                "blocked",
                "excluded",
            }:
                reasons.append("observed attempt has invalid disposition")
            if "failure" not in attempt:
                reasons.append("observed attempt must include its failure field")
            elif disposition == "failed" and not attempt.get("failure"):
                reasons.append("failed attempts require a failure description")
            elif disposition == "scored" and attempt.get("failure") is not None:
                reasons.append("scored attempts must have failure set to null")
            if disposition == "scored" and isinstance(candidate_id, str):
                scored_candidates.add(candidate_id)

    if len(set(observed_ids)) != len(observed_ids):
        reasons.append("observed attempt IDs must be unique")
    if len(set(observed_candidates)) != len(observed_candidates):
        reasons.append("observed attempt candidate IDs must be unique")
    observed_pairs = set(zip(observed_ids, observed_candidates, strict=False))
    if observed_pairs != planned_pairs or len(observed_pairs) != len(observed_ids):
        reasons.append("observed attempt IDs/candidates must exactly reconcile to the frozen plan")
    if not scored_candidates:
        reasons.append(f"{status} outcome requires at least one scored attempt")
    return planned_candidates, scored_candidates


def validate_outcome(outcome: Mapping[str, Any]) -> dict[str, Any]:
    """Fail closed on malformed or forged outcomes; never trust a winner field."""
    status = outcome.get("status")
    reasons: list[str] = []
    allowed = {"blocked", "inconclusive", "negative", "ready_for_human_review"}
    if not isinstance(status, str) or status not in allowed:
        reasons.append("invalid candidate confirmation status")
    if outcome.get("candidate_winner") is not None:
        reasons.append("winner is forbidden without validated selection and disjoint D3 evidence")
    if status == "blocked":
        if not isinstance(outcome.get("blockers"), list) or not outcome["blockers"]:
            reasons.append("blocked outcome requires explicit blockers")
        if outcome.get("candidates_scored") is not False:
            reasons.append("blocked outcome must state that no candidates were scored")
        if outcome.get("candidates_preregistered") is not False:
            reasons.append("blocked outcome must state that no candidates were preregistered")
        if outcome.get("attempts") != []:
            reasons.append("blocked outcome must contain no candidate attempts")
    elif isinstance(status, str) and status in {
        "inconclusive",
        "negative",
        "ready_for_human_review",
    }:
        if outcome.get("candidates_scored") is not True:
            reasons.append(f"{status} outcome requires completed candidate scoring")
        if outcome.get("candidates_preregistered") is not True:
            reasons.append(f"{status} outcome requires a frozen preregistration")
        if (
            not _is_sha256(outcome.get("protocol_sha256"))
            or outcome.get("protocol_sha256") != protocol_sha256()
        ):
            reasons.append(f"{status} outcome requires a valid frozen protocol SHA-256")
        if not _is_sha256(outcome.get("evaluation_report_sha256")):
            reasons.append(f"{status} outcome requires a valid evaluation report SHA-256")
        manifest_candidates, scored_candidates = _validate_completed_evidence(
            outcome, status, reasons
        )
        if (
            status == "inconclusive"
            and outcome.get("selection_disposition") != "criteria_unresolved"
        ):
            reasons.append(
                "inconclusive outcome requires criteria_unresolved selection disposition"
            )
        if status == "negative" and outcome.get("selection_disposition") != "criteria_failed":
            reasons.append("negative outcome requires criteria_failed selection disposition")
        if status == "ready_for_human_review":
            if outcome.get("frozen_criteria_passed") is not True:
                reasons.append("human-review outcome requires all frozen criteria to pass")
            if outcome.get("selection_disposition") != "criteria_passed":
                reasons.append(
                    "human-review outcome requires criteria_passed selection disposition"
                )
            if (
                not isinstance(outcome.get("proposed_candidate_id"), str)
                or not outcome["proposed_candidate_id"].strip()
            ):
                reasons.append("human-review outcome requires a nonempty proposed_candidate_id")
            elif outcome["proposed_candidate_id"] not in manifest_candidates:
                reasons.append(
                    "proposed_candidate_id must be present in the frozen candidate manifest"
                )
            elif outcome["proposed_candidate_id"] not in scored_candidates:
                reasons.append("proposed candidate must have a scored attempt")
            if (
                outcome.get("human_review_only") is not True
                or outcome.get("automatic_promotion") is not False
            ):
                reasons.append(
                    "human-review outcome must be review-only with automatic promotion disabled"
                )
    return {
        "valid": not reasons,
        "status": status if not reasons else "blocked",
        "candidate_winner": None,
        "reasons": reasons,
    }


def _valid_parity_report(parity: object) -> bool:
    if not isinstance(parity, Mapping):
        return False
    gates = parity.get("gates")
    return bool(
        type(parity.get("schema_version")) is int
        and parity.get("schema_version") == 1
        and type(parity.get("issue")) is int
        and parity.get("issue") == 399
        and parity.get("status") == "ready_for_replay"
        and isinstance(gates, Mapping)
        and gates.get("corpus") == "passed"
        and gates.get("eligible_mature_production_policy_evidence") == "passed"
        and parity.get("blockers") == []
        and parity.get("canonical_skill_claim") is False
        and parity.get("metrics") is None
    )


def build_report(
    parity: Mapping[str, Any] | None,
    *,
    parity_path: str | None = None,
    parity_sha256: str | None = None,
) -> dict[str, Any]:
    parity_ready = _valid_parity_report(parity)
    blockers = []
    if not parity_ready:
        blockers.append(
            {
                "code": "parity_diagnosis_unavailable",
                "detail": "Issue #399 production-parity diagnosis is not ready; #397 has no admissible corpus and #398 has no accumulated eligible mature evidence.",
            }
        )
    blockers.append(
        {
            "code": "candidate_family_not_preregistered",
            "detail": "Freeze, hash, and archive the bounded candidate-family preregistration and protocol before scoring; this readiness command accepts no candidate registry or completed evaluation evidence.",
        }
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "issue": 400,
        "status": "blocked",
        "blockers": blockers,
        "candidates_preregistered": False,
        "candidates_scored": False,
        "attempts": [],
        "candidate_winner": None,
        "skill_inference": False,
        "metrics": None,
        "evidence": {"parity_report": {"path": parity_path, "sha256": parity_sha256}},
        "protocol_sha256": protocol_sha256(),
        "protocol_to_freeze_before_scoring": PROTOCOL,
        "selection_prerequisites": [
            "Admissible versioned point-in-time corpus and passing production-parity diagnosis (#397/#399).",
            "Accumulated eligible, exact-key, mature production-parity evidence with all attempts and failures (#398/#399).",
            "Immutable preregistration and hashes for hypotheses, bounded candidate family, outcomes, horizons, folds, minimum effects, stopping rule, and thresholds.",
            "Exact shared walk-forward keys, existing final-selection gates, dependence-aware paired moving-block uncertainty, and family-wide multiplicity adjustment.",
        ],
        "d3_shadow_prerequisites": [
            "Selection passes all frozen criteria with complete attempt/failure accounting and no reused final holdout.",
            "Freeze selected configuration, policy identity, start/end, cutoff, and stopping rule before D3 begins.",
            "Run disjoint prospective shadow confirmation for at least 30 days with at least 200 exact mature pairs per horizon and no unreported failures.",
            "Human review of immutable evidence; production is unchanged.",
        ],
        "human_review_only": True,
        "production_changes": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parity-report", type=Path, default=DEFAULT_PARITY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    parity_hash = None
    try:
        parity_bytes = args.parity_report.read_bytes()
        parity_hash = hashlib.sha256(parity_bytes).hexdigest()
        parity = json.loads(parity_bytes.decode("utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        parity = None
    report = build_report(
        parity if isinstance(parity, Mapping) else None,
        parity_path=str(args.parity_report),
        parity_sha256=parity_hash,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
