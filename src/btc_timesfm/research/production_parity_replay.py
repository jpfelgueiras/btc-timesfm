"""Fail-closed readiness report for production-parity replay (roadmap v10 #399).

This command audits evidence availability only. It does not run a replay or
interpret metrics unless the corpus, production policy, and mature paired
forecast evidence have passed their independent gates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 1
DEFAULT_CORPUS_AUDIT = Path("docs/research/ISSUE_397_CANONICAL_BENCHMARK_AUDIT.json")
DEFAULT_OUTPUT = Path("docs/research/ISSUE_399_PRODUCTION_PARITY_BLOCKED.json")
TARGET_HOURS = 32_136
WARMUP_HOURS = 4_320
SHADOW_EVIDENCE_SCHEMA_VERSION = 6
SHADOW_FORECAST_SCHEMA_VERSION = 1
SHADOW_HORIZONS = ("2h", "4h", "8h", "16h")


def _frozen_cohort_blocker(cohort: Mapping[str, Any] | None) -> str | None:
    """Fail closed on frozen prospective-cohort evidence; does not establish skill."""
    if not isinstance(cohort, Mapping) or cohort.get("schema_version") != 1:
        return "Frozen prospective cohort report is missing or has an unsupported schema."
    contract = cohort.get("cohort")
    if not isinstance(contract, Mapping):
        return "Frozen cohort contract is missing."
    try:
        from datetime import datetime, timedelta

        cutoff = datetime.fromisoformat(str(contract["origin_cutoff_at"]).replace("Z", "+00:00"))
        as_of = datetime.fromisoformat(str(contract["evaluation_as_of"]).replace("Z", "+00:00"))
        if cutoff.tzinfo is None or as_of.tzinfo is None or cutoff > as_of - timedelta(hours=16):
            return (
                "Frozen cohort timestamps must be timezone-aware with a 16-hour maturity boundary."
            )
    except (KeyError, TypeError, ValueError):
        return "Frozen cohort timestamps are invalid."
    counts = cohort.get("pairs_by_horizon")
    if not isinstance(counts, Mapping) or set(counts) != set(SHADOW_HORIZONS):
        return "Frozen cohort must report exact per-horizon pair counts."
    expected = matured = 0
    for horizon in SHADOW_HORIZONS:
        bucket = counts[horizon]
        if not isinstance(bucket, Mapping) or any(
            not _exact_int(bucket.get(key)) for key in ("expected", "matured", "missing")
        ):
            return "Frozen cohort pair counts are invalid."
        if bucket["matured"] + bucket["missing"] != bucket["expected"]:
            return "Frozen cohort pair counts do not reconcile."
        expected += bucket["expected"]
        matured += bucket["matured"]
    if (
        not expected
        or matured != expected
        or cohort.get("failures") != []
        or cohort.get("ready") is not True
    ):
        return "Frozen cohort is incomplete or contains in-cohort failures."
    for key in ("database_sha256", "code_sha256", "cohort_sha256"):
        digest = cohort.get(key)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            return f"Frozen cohort {key} is missing or invalid."
    return None


def _load(path: Path | None) -> tuple[Mapping[str, Any] | None, str | None, str | None]:
    if path is None:
        return None, None, None
    try:
        content = path.read_bytes()
        content_hash = hashlib.sha256(content).hexdigest()
        raw = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"cannot read {path}: {exc}", None
    if not isinstance(raw, Mapping):
        return None, f"{path} must contain a JSON object", content_hash
    return raw, None, content_hash


def _exact_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _corpus_ready(corpus: Mapping[str, Any] | None) -> bool:
    if corpus is None or corpus.get("status") != "ready_for_replay":
        return False
    target = corpus.get("target_period")
    warmup = corpus.get("warmup_period")
    return bool(
        isinstance(target, Mapping)
        and _exact_int(target.get("expected_hours"))
        and target.get("expected_hours") == TARGET_HOURS
        and _exact_int(target.get("observed_hours"))
        and target.get("observed_hours") == TARGET_HOURS
        and _exact_int(target.get("missing_hours"))
        and target.get("missing_hours") == 0
        and _exact_int(corpus.get("target_period_observations"))
        and corpus.get("target_period_observations") == TARGET_HOURS
        and isinstance(warmup, Mapping)
        and _exact_int(warmup.get("expected_hours"))
        and warmup.get("expected_hours") == WARMUP_HOURS
        and _exact_int(warmup.get("observed_hours"))
        and warmup.get("observed_hours") == WARMUP_HOURS
        and _exact_int(warmup.get("missing_hours"))
        and warmup.get("missing_hours") == 0
        and _exact_int(corpus.get("gaps"))
        and corpus.get("gaps") == 0
        and _exact_int(corpus.get("duplicates"))
        and corpus.get("duplicates") == 0
        and _exact_int(corpus.get("invalid_rows"))
        and corpus.get("invalid_rows") == 0
        and corpus.get("errors") == []
        and isinstance(corpus.get("venue"), str)
        and bool(corpus["venue"].strip())
        and isinstance(corpus.get("pair"), str)
        and corpus.get("pair") in {"BTC/USD", "XBT/USD", "BTCUSD", "XBTUSD"}
        and isinstance(corpus.get("source_file"), str)
        and bool(corpus["source_file"].strip())
        and isinstance(corpus.get("source_sha256"), str)
        and len(corpus["source_sha256"]) == 64
        and all(character in "0123456789abcdefABCDEF" for character in corpus["source_sha256"])
        and corpus.get("vintage_column_present") is True
        and corpus.get("revision_column_present") is True
    )


def _ledger_blocker(ledger: Mapping[str, Any] | None) -> str | None:
    """Validate build_shadow_status output plus explicit parity attestation.

    The ledger exporter itself supplies evidence counts, not parity proof. The
    attestation must be an explicit true boolean, with any object scoped to the
    same policy identity.
    """
    if ledger is None:
        return "#398 build_shadow_status report is missing or malformed."
    champion = ledger.get("champion")
    evidence = ledger.get("evidence")
    if (
        ledger.get("schema_version") != 1
        or type(ledger.get("schema_version")) is not int
        or evidence is None
        or not isinstance(evidence, Mapping)
        or evidence.get("schema_version") != SHADOW_EVIDENCE_SCHEMA_VERSION
        or type(evidence.get("schema_version")) is not int
        or evidence.get("forecast_schema_version") != SHADOW_FORECAST_SCHEMA_VERSION
        or type(evidence.get("forecast_schema_version")) is not int
    ):
        return "#398 report or evidence schema version is unsupported."
    policy_id = ledger.get("policy_id")
    if not isinstance(policy_id, str) or not policy_id.strip():
        return "#398 report must name its shadow policy_id."
    if (
        not isinstance(champion, Mapping)
        or not isinstance(champion.get("configuration_id"), str)
        or not champion["configuration_id"].strip()
        or not isinstance(champion.get("name"), str)
        or not champion["name"].strip()
    ):
        return "#398 report must identify a named champion configuration."
    attestation = ledger.get("policy_parity_attestation")
    if ledger.get("policy_parity_attested") is not True and not (
        isinstance(attestation, Mapping)
        and attestation.get("attested") is True
        and attestation.get("policy_id") == policy_id
    ):
        return "An explicit policy parity attestation scoped to policy_id is required."

    names = (
        "confirmatory_forecasts",
        "expected_pairs",
        "matured_pairs",
        "failures",
        "failed_attempt_pairs",
    )
    if any(not _exact_int(evidence.get(name)) for name in names):
        return "#398 evidence counts must be exact nonnegative integers."
    confirmatory = evidence["confirmatory_forecasts"]
    expected = evidence["expected_pairs"]
    matured = evidence["matured_pairs"]
    failures = evidence["failures"]
    failed = evidence["failed_attempt_pairs"]
    missing = evidence.get("missing_pairs_by_horizon")
    versions = evidence.get("forecast_versions")
    if confirmatory == 0 or expected != confirmatory * len(SHADOW_HORIZONS) + failed:
        return "#398 expected pair count is inconsistent with forecasts and failed attempts."
    if matured > expected or failed > expected or failures > failed:
        return "#398 matured/failure counts exceed their coherent pair totals."
    if (
        not isinstance(missing, Mapping)
        or set(missing) != set(SHADOW_HORIZONS)
        or any(not _exact_int(missing.get(horizon)) for horizon in SHADOW_HORIZONS)
        or sum(missing.values()) != expected - matured
    ):
        return "#398 missing-pair horizon counts do not reconcile to expected minus matured pairs."
    if (
        not isinstance(versions, Mapping)
        or any(not _exact_int(count) for count in versions.values())
        or sum(versions.values()) != confirmatory
        or not versions
    ):
        return "#398 forecast version counts do not reconcile to confirmatory forecasts."
    if type(evidence.get("maturity_fraction")) not in (int, float) or not math.isfinite(
        evidence["maturity_fraction"]
    ):
        return "#398 maturity_fraction must be numeric and consistent with pair counts."
    fraction = evidence["maturity_fraction"]
    expected_fraction = matured / expected if expected else None
    if expected_fraction is None or abs(fraction - expected_fraction) > 1e-12:
        return "#398 maturity_fraction does not match matured/expected pairs."
    if matured != expected or failed != 0 or failures != 0:
        return "All expected confirmatory pairs must be mature with no failed attempts for replay readiness."
    return None


def build_report(
    corpus_path: Path, ledger_path: Path | None, frozen_cohort_path: Path | None = None
) -> dict[str, Any]:
    """Build a reproducible machine-readable readiness report from evidence files."""
    corpus, corpus_error, corpus_hash = _load(corpus_path)
    ledger, ledger_error, ledger_hash = _load(ledger_path)
    frozen_cohort, frozen_error, frozen_hash = _load(frozen_cohort_path)
    blockers: list[dict[str, str]] = []
    corpus_ready = _corpus_ready(corpus)
    if corpus_error:
        blockers.append({"code": "corpus_audit_unreadable", "detail": corpus_error})
    elif not corpus_ready:
        blockers.append(
            {
                "code": "canonical_corpus_ineligible",
                "detail": "Require ready_for_replay plus exact 32,136 target and 4,320 warm-up coverage, zero gaps/duplicates/errors, named USD venue/pair, source hash, and vintage/revision provenance (issue #397).",
            }
        )
    if ledger_path is None:
        blockers.append(
            {
                "code": "prospective_ledger_evidence_missing",
                "detail": "Export the #398 prospective shadow ledger with exact origins, horizons, policy/configuration identity, failures, and matured actuals; then rerun with --ledger.",
            }
        )
    elif ledger_error:
        blockers.append({"code": "prospective_ledger_unreadable", "detail": ledger_error})
    elif (ledger_problem := _ledger_blocker(ledger)) is not None:
        blockers.append(
            {
                "code": "prospective_ledger_not_eligible",
                "detail": ledger_problem,
            }
        )
    frozen_problem = (
        frozen_error
        if frozen_error
        else _frozen_cohort_blocker(frozen_cohort)
        if frozen_cohort_path is not None
        else None
    )

    ready = not blockers
    return {
        "schema_version": SCHEMA_VERSION,
        "issue": 399,
        "status": "ready_for_replay" if ready else "blocked",
        "canonical_skill_claim": False,
        "metrics_interpretable_as_canonical": False,
        "metrics": None,
        "metric_status": "not_computed_blocked" if not ready else "replay_not_run",
        "gates": {
            "corpus": "passed" if corpus_ready else "blocked",
            "eligible_mature_production_policy_evidence": "passed"
            if ledger is not None and _ledger_blocker(ledger) is None
            else "blocked",
        },
        "evidence": {
            "corpus_audit": {"path": str(corpus_path), "sha256": corpus_hash},
            "prospective_ledger": {
                "path": str(ledger_path) if ledger_path else None,
                "sha256": ledger_hash,
                "policy_id": ledger.get("policy_id") if ledger is not None else None,
                "champion_configuration_id": (
                    ledger.get("champion", {}).get("configuration_id")
                    if ledger is not None and isinstance(ledger.get("champion"), Mapping)
                    else None
                ),
                "confirmatory_forecasts": (
                    ledger.get("evidence", {}).get("confirmatory_forecasts")
                    if ledger is not None and isinstance(ledger.get("evidence"), Mapping)
                    else None
                ),
                "matured_pairs": (
                    ledger.get("evidence", {}).get("matured_pairs")
                    if ledger is not None and isinstance(ledger.get("evidence"), Mapping)
                    else None
                ),
            },
            "frozen_prospective_cohort": {
                "path": str(frozen_cohort_path) if frozen_cohort_path else None,
                "sha256": frozen_hash,
                "ready": frozen_cohort is not None
                and _frozen_cohort_blocker(frozen_cohort) is None,
                "canonical_skill_claim": False,
            },
        },
        "frozen_cohort_readiness": {
            "status": "not_requested"
            if frozen_cohort_path is None
            else "ready"
            if frozen_problem is None
            else "blocked",
            "blocker": frozen_problem,
        },
        "blockers": blockers,
        "required_comparisons": [
            "production_policy",
            "persistence",
            "seasonal_naive",
            "strongest_simple_baseline",
        ],
        "required_shared_key": ["origin_at", "target_at", "horizon"],
        "required_metrics": ["mae", "bias", "direction_accuracy", "samples", "failures"],
        "stratification": ["horizon", "regime"],
        "inference_requirements": {
            "dependence_aware_uncertainty": "moving-block paired resampling over exact shared keys",
            "multiplicity": "predeclared family-wise adjustment across all comparisons and strata",
        },
        "replay_manifest_required": [
            "corpus_sha256",
            "ledger_sha256",
            "policy_id",
            "configuration",
            "code_revision",
            "command",
            "evaluation_cutoff",
        ],
        "next_action": "Resolve blockers, preserve immutable evidence and hashes, then rerun this audit before executing replay.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-audit", type=Path, default=DEFAULT_CORPUS_AUDIT)
    parser.add_argument("--ledger", type=Path, help="#398 mature prospective ledger evidence JSON")
    parser.add_argument(
        "--frozen-cohort", type=Path, help="immutable #419 frozen prospective cohort report JSON"
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = build_report(args.corpus_audit, args.ledger, args.frozen_cohort)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
