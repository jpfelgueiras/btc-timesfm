"""Bounded, download-free eligibility and report harness for issue #344.

This module deliberately does not perform inference. It records why a candidate
can or cannot enter the staged experiment; real predictions require the frozen
benchmark corpus and an explicitly authorized, identity-verified artifact.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from btc_timesfm.research.canonical_benchmark import (
    TARGET_END,
    TARGET_START,
    TARGET_END_TS,
    TARGET_START_TS,
    WARMUP_START_TS,
)

MODEL_ID = "google/timesfm-3.0-pytorch"
MODEL_REVISION = "43046b85ec22d584a13f8098c2ed39c889e129c2"
CONTEXT_LENGTHS = (64, 168, 336, 512, 1024)
HORIZONS = (2, 4, 8, 16)
PRODUCTION_CONTEXTS = (168, 336, 512)
RUNTIME_VERIFIED_CONTEXTS = PRODUCTION_CONTEXTS


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
            supported_contexts=RUNTIME_VERIFIED_CONTEXTS,
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


def validate_corpus_manifest(manifest: dict[str, Any] | None) -> dict[str, Any]:
    """Validate the canonical audit fields required before replay eligibility."""
    failures: list[str] = []
    if not isinstance(manifest, dict):
        return {"eligible": False, "failures": ["validated canonical audit manifest is missing"]}
    if manifest.get("status") != "ready_for_replay":
        failures.append("corpus audit status is not ready_for_replay")
    digest = manifest.get("source_sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        failures.append("complete lowercase source SHA-256 is missing or invalid")
    if not isinstance(manifest.get("venue"), str) or not manifest["venue"].strip():
        failures.append("single-venue identity is missing")
    pair = manifest.get("pair")
    if not isinstance(pair, str) or pair.upper() not in {"BTC/USD", "XBT/USD", "BTCUSD", "XBTUSD"}:
        failures.append("corpus is not a BTC/USD pair")
    target = manifest.get("target_period")
    if not isinstance(target, dict) or (
        target.get("start_inclusive") != TARGET_START.isoformat()
        or target.get("end_exclusive") != TARGET_END.isoformat()
        or target.get("expected_hours") != (TARGET_END_TS - TARGET_START_TS) // 3600
        or target.get("observed_hours") != (TARGET_END_TS - TARGET_START_TS) // 3600
        or target.get("missing_hours") != 0
    ):
        failures.append("exact complete required target coverage is not proven")
    warmup = manifest.get("warmup_period")
    if not isinstance(warmup, dict) or (
        warmup.get("start_inclusive")
        != datetime.fromtimestamp(WARMUP_START_TS, timezone.utc).isoformat()
        or warmup.get("end_exclusive") != TARGET_START.isoformat()
        or warmup.get("expected_hours") != 180 * 24
        or warmup.get("observed_hours") != 180 * 24
        or warmup.get("missing_hours") != 0
    ):
        failures.append("exact complete 180-day warm-up coverage is not proven")
    if (
        manifest.get("invalid_rows") != 0
        or manifest.get("gaps") != 0
        or manifest.get("duplicates") != 0
    ):
        failures.append("corpus integrity audit reports missing or invalid records")
    return {"eligible": not failures, "failures": failures}


def validate_origin_metadata(
    metadata: dict[str, dict[str, Any]] | None, manifest: dict[str, Any] | None
) -> dict[str, Any]:
    """Bind origin lists to the audited source hash, venue, pair and target interval."""
    if not isinstance(metadata, dict) or not metadata:
        return {"paired": False, "failures": ["trusted origin metadata is missing"], "origins": []}
    if not isinstance(manifest, dict):
        return {"paired": False, "failures": ["origin source audit is missing"], "origins": []}
    failures: list[str] = []
    reference: list[str] | None = None
    for name, record in metadata.items():
        if not isinstance(record, dict):
            failures.append(f"origin metadata for {name} is invalid")
            continue
        for key in ("source_sha256", "venue", "pair", "target_start", "target_end"):
            expected = {
                "source_sha256": manifest.get("source_sha256"),
                "venue": manifest.get("venue"),
                "pair": manifest.get("pair"),
                "target_start": TARGET_START.isoformat(),
                "target_end": TARGET_END.isoformat(),
            }[key]
            if record.get(key) != expected:
                failures.append(f"origin metadata for {name} has mismatched {key}")
        origins = record.get("origins")
        if (
            not isinstance(origins, list)
            or not origins
            or any(not isinstance(x, str) for x in origins)
        ):
            failures.append(f"origin list for {name} is missing or invalid")
            continue
        for origin in origins:
            try:
                timestamp = datetime.fromisoformat(origin.replace("Z", "+00:00"))
                if (
                    timestamp.tzinfo is None
                    or timestamp.utcoffset() != timezone.utc.utcoffset(timestamp)
                    or timestamp.minute != 0
                    or timestamp.second != 0
                    or timestamp.microsecond != 0
                    or not TARGET_START <= timestamp < TARGET_END
                ):
                    raise ValueError
            except ValueError:
                failures.append(
                    f"origin list for {name} contains an invalid/out-of-range UTC origin"
                )
                break
        if len(set(origins)) != len(origins):
            failures.append(f"origin list for {name} contains duplicates")
        if reference is None:
            reference = origins
        elif origins != reference:
            failures.append(f"origin list for {name} does not exactly match paired candidates")
    return {
        "paired": not failures and reference is not None,
        "failures": failures,
        "origins": reference or [],
    }


def comparison_designs(
    runtime_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Separate policy comparisons from controlled equal-physical-lookback contrasts."""
    runtime_digest = runtime_contract.get("artifact_sha256") if runtime_contract else None
    verified = bool(
        runtime_contract
        and runtime_contract.get("status") == "verified"
        and runtime_contract.get("model_id") == MODEL_ID
        and runtime_contract.get("revision") == MODEL_REVISION
        and runtime_contract.get("api") == "TimesFM3Evaluator.predict_batch"
        and isinstance(runtime_digest, str)
        and len(runtime_digest) == 64
        and all(character in "0123456789abcdef" for character in runtime_digest)
    )
    contexts = set(runtime_contract.get("supported_contexts", ())) if verified else set()
    frequencies = set(runtime_contract.get("supported_frequencies", ())) if verified else set()
    lookbacks = runtime_contract.get("context_lookbacks_hours", {}) if verified else {}
    step_contexts = [n for n in CONTEXT_LENGTHS if n in contexts]
    common_values = {lookbacks.get(str(n)) for n in step_contexts}
    step_ready = (
        verified
        and bool(frequencies)
        and len(step_contexts) >= 2
        and len(common_values) == 1
        and None not in common_values
    )
    if not step_ready:
        step_contexts = []
    common = next(iter(common_values)) if step_ready else None
    return {
        "policy_context_comparison": {
            "status": "defined-policy-comparison",
            "contexts": list(CONTEXT_LENGTHS),
            "physical_lookback_hours": {str(n): n for n in CONTEXT_LENGTHS},
            "interpretation": "different production policies with different physical lookbacks",
        },
        "controlled_step_count_comparison": {
            "status": "eligible" if step_ready and len(step_contexts) >= 2 else "blocked",
            "contexts": step_contexts,
            "common_physical_lookback_hours": common if step_ready else None,
            "reason": None
            if step_ready
            else "verified common-frequency/lookback runtime contract is unavailable",
        },
    }


def assess_candidate(
    candidate: CheckpointCandidate,
    *,
    artifact_revision: str | None = None,
    artifact_sha256: str | None = None,
    corpus_audit: dict[str, Any] | None = None,
    available_returns: int | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    if not candidate.authorized:
        reasons.append("checkpoint is not explicitly configured and authorized")
    if not candidate.model_id or not candidate.revision:
        reasons.append("exact model id and immutable revision are unavailable")
    if artifact_revision != candidate.revision:
        reasons.append("loaded artifact revision does not exactly match catalog revision")
    if (
        candidate.known_artifact_sha256 is None
        or artifact_sha256 != candidate.known_artifact_sha256
    ):
        reasons.append("verified artifact SHA-256 identity is unavailable or mismatched")
    if not corpus_audit or corpus_audit.get("eligible") is not True:
        reasons.append("eligible frozen canonical corpus is unavailable")
    lengths = [
        length for length in candidate.supported_contexts if length in RUNTIME_VERIFIED_CONTEXTS
    ]
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
    *,
    audit_manifest: dict[str, Any] | None = None,
    origin_metadata: dict[str, dict[str, Any]] | None = None,
    runtime_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    corpus_audit = validate_corpus_manifest(audit_manifest)
    pairing = validate_origin_metadata(origin_metadata, audit_manifest)
    candidates = [assess_candidate(item, corpus_audit=corpus_audit) for item in candidate_catalog()]
    ready = [item for item in candidates if item["eligible"]]
    blocked_reasons = []
    if not corpus_audit["eligible"]:
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
            "context_runtime_status": {
                str(n): "verified" if n in RUNTIME_VERIFIED_CONTEXTS else "blocked-unverified"
                for n in CONTEXT_LENGTHS
            },
            **comparison_designs(runtime_contract),
            "same_origins_required": True,
            "nested_chronological_folds": True,
            "maximum_inner_selected_contexts": 2,
            "normalization_and_symmetric_averaging": "separate staged factors",
            "one_call_vs_horizon_specific": "compare only if outputs differ",
            "baseline": ["production context ensemble", "persistence"],
            "promotion": {
                "minimum_oos_mae_improvement": 0.03,
                "confidence": 0.95,
                "ci_must_exclude_no_improvement": True,
            },
        },
        "corpus": {
            **corpus_audit,
            "status": "available" if corpus_audit["eligible"] else "blocked",
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
