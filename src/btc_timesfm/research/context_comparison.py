"""Bounded, download-free eligibility and report harness for issue #344.

This module deliberately does not perform inference. It records why a candidate
can or cannot enter the staged experiment; real predictions require the frozen
benchmark corpus and an explicitly authorized, identity-verified artifact.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

MODEL_ID = "google/timesfm-3.0-pytorch"
MODEL_REVISION = "43046b85ec22d584a13f8098c2ed39c889e129c2"
CONTEXT_LENGTHS = (64, 168, 336, 512, 1024)
HORIZONS = (2, 4, 8, 16)
PRODUCTION_CONTEXTS = (168, 336, 512)


@dataclass(frozen=True)
class CheckpointCandidate:
    name: str
    model_id: str
    revision: str
    authorized: bool
    known_artifact_sha256: str | None
    supported_contexts: tuple[int, ...]
    status: str


def candidate_catalog() -> tuple[CheckpointCandidate, ...]:
    """Return the bounded catalog; no public checkpoint is implicitly authorized."""
    return (
        CheckpointCandidate(
            name="production-timesfm-3.0.2",
            model_id=MODEL_ID,
            revision=MODEL_REVISION,
            authorized=True,
            known_artifact_sha256=None,
            supported_contexts=CONTEXT_LENGTHS,
            status="configured-production-checkpoint",
        ),
        CheckpointCandidate(
            name="newer-timesfm-checkpoint",
            model_id="",
            revision="",
            authorized=False,
            known_artifact_sha256=None,
            supported_contexts=(),
            status="not-configured-or-authorized",
        ),
    )


def validate_origin_pairing(origin_sets: dict[str, tuple[str, ...]]) -> dict[str, Any]:
    """Require exact origin identity across compared candidates, preserving failures."""
    if not origin_sets:
        return {"paired": False, "reason": "no candidate origins supplied", "origins": []}
    names = tuple(origin_sets)
    reference = origin_sets[names[0]]
    if len(set(reference)) != len(reference):
        return {"paired": False, "reason": "duplicate origins", "origins": list(reference)}
    for name in names[1:]:
        if origin_sets[name] != reference:
            return {
                "paired": False,
                "reason": f"origin mismatch for {name}",
                "origins": list(reference),
            }
    return {"paired": True, "reason": None, "origins": list(reference)}


def assess_candidate(
    candidate: CheckpointCandidate,
    *,
    artifact_revision: str | None = None,
    artifact_sha256: str | None = None,
    corpus_eligible: bool = False,
    available_returns: int | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    if not candidate.authorized:
        reasons.append("checkpoint is not explicitly configured and authorized")
    if not candidate.model_id or not candidate.revision:
        reasons.append("exact model id and immutable revision are unavailable")
    if artifact_revision != candidate.revision:
        reasons.append("loaded artifact revision does not exactly match catalog revision")
    if candidate.known_artifact_sha256 is None or artifact_sha256 != candidate.known_artifact_sha256:
        reasons.append("verified artifact SHA-256 identity is unavailable or mismatched")
    if not corpus_eligible:
        reasons.append("eligible frozen canonical corpus is unavailable")
    lengths = [length for length in candidate.supported_contexts if length in CONTEXT_LENGTHS]
    if available_returns is not None:
        lengths = [length for length in lengths if length <= available_returns]
    return {
        **asdict(candidate),
        "supported_contexts": lengths,
        "physical_lookback_candles": {str(length): length + 1 for length in lengths},
        "eligible": not reasons,
        "blocked_reasons": reasons,
    }


def build_report(
    *, corpus_eligible: bool = False, origin_sets: dict[str, tuple[str, ...]] | None = None
) -> dict[str, Any]:
    candidates = [
        assess_candidate(item, corpus_eligible=corpus_eligible) for item in candidate_catalog()
    ]
    pairing = validate_origin_pairing(origin_sets or {})
    ready = [item for item in candidates if item["eligible"]]
    blocked_reasons = []
    if not corpus_eligible:
        blocked_reasons.append("#339 canonical BTC/USD corpus is blocked or absent")
    if not pairing["paired"]:
        blocked_reasons.append("paired candidate origins have not been supplied and validated")
    if not ready:
        blocked_reasons.append(
            "no checkpoint passes exact artifact identity and authorization gates"
        )
    return {
        "schema_version": 1,
        "experiment": "issue-344-timesfm-context-horizon-checkpoint-comparison",
        "status": "blocked" if blocked_reasons else "ready-for-inference",
        "production": {
            "model_id": MODEL_ID,
            "revision": MODEL_REVISION,
            "contexts": list(PRODUCTION_CONTEXTS),
            "horizons": list(HORIZONS),
            "device": "cpu",
            "batch_size": 1,
            "return_quantiles": True,
            "symmetric_averaging": False,
        },
        "design": {
            "contexts": list(CONTEXT_LENGTHS),
            "physical_lookback_candles": {str(n): n + 1 for n in CONTEXT_LENGTHS},
            "same_origins_required": True,
            "nested_chronological_folds": True,
            "maximum_inner_selected_contexts": 2,
            "normalization_and_symmetric_averaging": "separate staged factors",
            "one_call_vs_horizon_specific": "compare only if outputs differ",
            "baseline": ["production context ensemble", "persistence"],
            "promotion": {"minimum_oos_mae_improvement": 0.03, "ci_required": True},
        },
        "corpus": {
            "eligible": corpus_eligible,
            "status": "available" if corpus_eligible else "blocked",
        },
        "origin_pairing": pairing,
        "candidates": candidates,
        "blocked_reasons": blocked_reasons,
        "inference_performed": False,
        "forecast_skill_claimed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("context_comparison_report.json"))
    args = parser.parse_args()
    report = build_report()
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Context comparison status: {report['status']}; report: {args.output}")


if __name__ == "__main__":
    main()
