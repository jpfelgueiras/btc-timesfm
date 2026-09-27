"""Fail-closed readiness report for production-parity replay (roadmap v11 #411).

This command audits evidence availability only. It does not run a replay or
interpret metrics unless the corpus, production policy, and mature paired
forecast evidence have passed their independent gates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from btc_timesfm.research.frozen_scoring_protocol import PROTOCOL_PATH, load_frozen_protocol

SCHEMA_VERSION = 1
DEFAULT_CORPUS_AUDIT = Path("docs/research/ISSUE_397_CANONICAL_BENCHMARK_AUDIT.json")
DEFAULT_OUTPUT = Path("docs/research/ISSUE_399_PRODUCTION_PARITY_BLOCKED.json")
DEFAULT_ISSUE_411_OUTPUT = Path("docs/research/ISSUE_411_PARITY_BASELINES_BLOCKED.json")
TARGET_HOURS = 32_136
WARMUP_HOURS = 4_320
SHADOW_EVIDENCE_SCHEMA_VERSION = 6
SHADOW_FORECAST_SCHEMA_VERSION = 1
SHADOW_HORIZONS = ("2h", "4h", "8h", "16h")


def _frozen_cohort_blocker(cohort: Mapping[str, Any] | None) -> str | None:
    """Validate every frozen-cohort identity, count, and content hash; no skill inference."""
    if (
        not isinstance(cohort, Mapping)
        or type(cohort.get("schema_version")) is not int
        or cohort.get("schema_version") != 1
    ):
        return "Frozen prospective cohort report is missing or has an unsupported schema."
    contract = cohort.get("cohort")
    if not isinstance(contract, Mapping):
        return "Frozen cohort contract is missing."
    try:
        cutoff = datetime.fromisoformat(str(contract["origin_cutoff_at"]).replace("Z", "+00:00"))
        as_of = datetime.fromisoformat(str(contract["evaluation_as_of"]).replace("Z", "+00:00"))
        if (
            cutoff.tzinfo is None
            or as_of.tzinfo is None
            or cutoff.utcoffset() != timedelta(0)
            or as_of.utcoffset() != timedelta(0)
            or cutoff > as_of - timedelta(hours=16)
            or contract.get("horizons") != list(SHADOW_HORIZONS)
            or contract.get("maximum_horizon_hours") != 16
            or contract.get("rule")
            != "origin <= cutoff and origin + 16h <= evaluation_as_of; exact UTC targets"
        ):
            return "Frozen cohort timestamps must be UTC with a 16-hour maturity boundary."
    except (KeyError, TypeError, ValueError):
        return "Frozen cohort timestamps are invalid."
    counts = cohort.get("pairs_by_horizon")
    if not isinstance(counts, Mapping) or set(counts) != set(SHADOW_HORIZONS):
        return "Frozen cohort must report exact per-horizon pair counts."
    pairs = cohort.get("pairs")
    failures = cohort.get("failures")
    provenance_blockers = cohort.get("provenance_blockers")
    blocked_reasons = cohort.get("blocked_reasons")
    failure_only_pairs = cohort.get("failure_only_pairs")
    failed_pair_identities = cohort.get("failed_pair_identities")
    identities = cohort.get("lineage_identities")
    if (
        not isinstance(pairs, list)
        or not isinstance(failures, list)
        or not isinstance(provenance_blockers, list)
        or not isinstance(blocked_reasons, list)
    ):
        return "Frozen cohort must include non-empty exact pairs and a failure list."
    if (
        not isinstance(failure_only_pairs, list)
        or not isinstance(failed_pair_identities, list)
        or not isinstance(identities, list)
    ):
        return "Frozen cohort pair/failure lineage records are missing."
    seen_pairs: set[tuple[str, datetime, str]] = set()
    derived_identities: set[tuple[str, ...]] = set()
    forecast_origins: set[tuple[str, datetime]] = set()
    horizons_by_origin: dict[tuple[str, datetime], set[str]] = {}
    pair_counts = {horizon: {"expected": 0, "matured": 0} for horizon in SHADOW_HORIZONS}
    try:
        for pair in pairs:
            if not isinstance(pair, Mapping):
                return "Frozen cohort contains a malformed pair."
            origin = datetime.fromisoformat(str(pair["origin_at"]).replace("Z", "+00:00"))
            target = datetime.fromisoformat(str(pair["target_at"]).replace("Z", "+00:00"))
            if (
                origin.tzinfo is None
                or target.tzinfo is None
                or origin.utcoffset() != timedelta(0)
                or target.utcoffset() != timedelta(0)
            ):
                return "Frozen cohort pair timestamps must be explicit UTC."
            origin = origin.astimezone(timezone.utc)
            target = target.astimezone(timezone.utc)
            if origin > cutoff or origin > as_of - timedelta(hours=16):
                return "Frozen cohort pair violates its cutoff or 16-hour maturity boundary."
            horizon = pair.get("horizon")
            if horizon not in SHADOW_HORIZONS:
                return "Frozen cohort contains an unsupported horizon."
            if target != origin + timedelta(hours=int(horizon[:-1])):
                return "Frozen cohort pair target does not match its exact origin/horizon."
            config_id = pair.get("configuration_id")
            policy_id = pair.get("policy_id")
            data_id = pair.get("data_lineage_id")
            forecast_sha = pair.get("forecast_sha256")
            model_identity = pair.get("model_identity")
            if (
                not all(
                    isinstance(value, str) and value
                    for value in (config_id, policy_id, data_id, forecast_sha)
                )
                or not isinstance(model_identity, Mapping)
                or any(
                    not isinstance(model_identity.get(key), str) or not model_identity[key]
                    for key in ("id", "revision", "package", "package_version")
                )
            ):
                return "Frozen cohort pair lineage identity is incomplete."
            key = (config_id, origin, horizon)
            if key in seen_pairs:
                return "Frozen cohort contains a duplicate UTC forecast/horizon identity."
            seen_pairs.add(key)
            forecast_origins.add((config_id, origin))
            horizons_by_origin.setdefault((config_id, origin), set()).add(horizon)
            policy_previous = next(
                (identity[1] for identity in derived_identities if identity[0] == config_id),
                policy_id,
            )
            if policy_previous != policy_id:
                return "Frozen cohort changes policy identity within one configuration."
            derived_identities.add(
                (
                    config_id,
                    policy_id,
                    data_id,
                    forecast_sha,
                    *(
                        model_identity[key]
                        for key in ("id", "revision", "package", "package_version")
                    ),
                )
            )
            pair_counts[horizon]["expected"] += 1
            if type(pair.get("matured")) is not bool:
                return "Frozen cohort maturity flags must be booleans."
            if pair["matured"]:
                pair_counts[horizon]["matured"] += 1
        failure_only_keys: set[tuple[str, datetime, str]] = set()
        for failed_pair in failure_only_pairs:
            if not isinstance(failed_pair, Mapping):
                return "Frozen cohort contains a malformed failed-pair identity."
            origin = datetime.fromisoformat(str(failed_pair["origin_at"]).replace("Z", "+00:00"))
            horizon = failed_pair["horizon"]
            if (
                origin.tzinfo is None
                or origin.utcoffset() != timedelta(0)
                or horizon not in SHADOW_HORIZONS
                or not isinstance(failed_pair.get("configuration_id"), str)
                or not failed_pair["configuration_id"]
            ):
                return "Frozen cohort failed-pair identity is invalid."
            key = (
                failed_pair["configuration_id"],
                origin.astimezone(timezone.utc),
                horizon,
            )
            if key in failure_only_keys or (key[0], key[1]) in forecast_origins:
                return "Frozen cohort failed-pair denominator is duplicated or overlaps a forecast."
            failure_only_keys.add(key)
            pair_counts[horizon]["expected"] += 1
        reported_failed_keys: set[tuple[str, datetime, str]] = set()
        for failed_pair in failed_pair_identities:
            if not isinstance(failed_pair, Mapping):
                return "Frozen cohort contains a malformed failed-pair identity."
            origin = datetime.fromisoformat(str(failed_pair["origin_at"]).replace("Z", "+00:00"))
            config_id, horizon = failed_pair.get("configuration_id"), failed_pair.get("horizon")
            if (
                origin.tzinfo is None
                or origin.utcoffset() != timedelta(0)
                or not isinstance(config_id, str)
                or not config_id
                or horizon not in SHADOW_HORIZONS
            ):
                return "Frozen cohort failed-pair identity is invalid."
            failed_key = (config_id, origin.astimezone(timezone.utc), horizon)
            if failed_key in reported_failed_keys:
                return "Frozen cohort failed-pair identities contain duplicates."
            reported_failed_keys.add(failed_key)
        failure_derived_keys: set[tuple[str, datetime, str]] = set()
        for failure in failures:
            if not isinstance(failure, Mapping):
                return "Frozen cohort contains a malformed failure row."
            origin = datetime.fromisoformat(str(failure["origin_at"]).replace("Z", "+00:00"))
            observed_at = datetime.fromisoformat(str(failure["observed_at"]).replace("Z", "+00:00"))
            horizons = json.loads(str(failure.get("expected_horizons_json") or "null"))
            config_id = failure.get("configuration_id")
            if (
                origin.tzinfo is None
                or origin.utcoffset() != timedelta(0)
                or observed_at.tzinfo is None
                or observed_at.utcoffset() != timedelta(0)
                or origin > cutoff
                or origin > as_of - timedelta(hours=16)
                or observed_at > as_of
                or not isinstance(config_id, str)
                or not config_id
                or not isinstance(failure.get("stage"), str)
                or not failure["stage"]
                or not isinstance(horizons, list)
                or not horizons
                or any(horizon not in SHADOW_HORIZONS for horizon in horizons)
            ):
                return "Frozen cohort failure content is invalid."
            failure_derived_keys.update(
                (config_id, origin.astimezone(timezone.utc), horizon) for horizon in horizons
            )
        if failure_derived_keys != reported_failed_keys:
            return "Frozen cohort failed-pair identities do not reconcile to failure rows."
        if any(
            horizons_by_origin[origin_key] != set(SHADOW_HORIZONS)
            for origin_key in forecast_origins
        ):
            return "Frozen cohort forecast origin is missing a supported horizon."
        if failure_only_keys != {
            key for key in reported_failed_keys if (key[0], key[1]) not in forecast_origins
        }:
            return "Frozen cohort failure-only denominator does not reconcile to forecast origins."
        expected = matured = 0
        for horizon in SHADOW_HORIZONS:
            bucket = counts[horizon]
            if not isinstance(bucket, Mapping) or any(
                not _exact_int(bucket.get(key)) for key in ("expected", "matured", "missing")
            ):
                return "Frozen cohort pair counts are invalid."
            derived_expected = pair_counts[horizon]["expected"]
            derived_matured = pair_counts[horizon]["matured"]
            if (
                bucket["expected"] != derived_expected
                or bucket["matured"] != derived_matured
                or bucket["missing"] != derived_expected - derived_matured
            ):
                return "Frozen cohort per-horizon counts do not match its exact pair identities."
            expected += bucket["expected"]
            matured += bucket["matured"]
        if (
            not _exact_int(cohort.get("expected_pairs"))
            or not _exact_int(cohort.get("matured_pairs"))
            or cohort.get("expected_pairs") != expected
            or cohort.get("matured_pairs") != matured
        ):
            return "Frozen cohort aggregate pair counts do not reconcile."
        if not _exact_int(cohort.get("eligible_origins")) or cohort.get("eligible_origins") != len(
            forecast_origins
        ):
            return "Frozen cohort eligible-origin count does not reconcile."
        missing_by_horizon = cohort.get("missing_pairs_by_horizon")
        if (
            not isinstance(missing_by_horizon, Mapping)
            or set(missing_by_horizon) != set(SHADOW_HORIZONS)
            or any(not _exact_int(missing_by_horizon.get(h)) for h in SHADOW_HORIZONS)
            or dict(missing_by_horizon)
            != {h: pair_counts[h]["expected"] - pair_counts[h]["matured"] for h in SHADOW_HORIZONS}
        ):
            return "Frozen cohort missing-horizon counts do not reconcile."
        if sorted([list(identity) for identity in derived_identities]) != identities:
            return "Frozen cohort lineage identity list does not reconcile to its pairs."
    except (KeyError, TypeError, ValueError, OverflowError):
        return "Frozen cohort pair or failure identities are malformed."
    computed_ready = bool(
        expected
        and matured == expected
        and not failures
        and not provenance_blockers
        and not blocked_reasons
    )
    if type(cohort.get("ready")) is not bool or cohort.get("ready") is not computed_ready:
        return "Frozen cohort readiness flag does not match its blockers and pair counts."
    if (
        cohort.get("status") != ("ready" if computed_ready else "blocked")
        or cohort.get("metrics_computed") is not False
    ):
        return "Frozen cohort status or metrics-computed declaration is invalid."
    for key in ("database_sha256", "code_sha256", "cohort_sha256"):
        digest = cohort.get(key)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            return f"Frozen cohort {key} is missing or invalid."
    if not _exact_int(cohort.get("post_cutoff_forecasts")) or not _exact_int(
        cohort.get("right_censored_forecasts")
    ):
        return "Frozen cohort censored-forecast counts are invalid."
    if cohort["post_cutoff_forecasts"] > cohort["right_censored_forecasts"]:
        return "Frozen cohort post-cutoff forecasts exceed the right-censored forecast total."
    censored_pairs = cohort.get("right_censored_pairs_by_horizon")
    failed_counts = cohort.get("failed_pairs_by_horizon")
    if (
        not isinstance(censored_pairs, Mapping)
        or set(censored_pairs) != set(SHADOW_HORIZONS)
        or any(not _exact_int(censored_pairs.get(h)) for h in SHADOW_HORIZONS)
        or any(censored_pairs[h] > cohort["right_censored_forecasts"] for h in SHADOW_HORIZONS)
        or not isinstance(failed_counts, Mapping)
        or set(failed_counts) != set(SHADOW_HORIZONS)
        or any(not _exact_int(failed_counts.get(h)) for h in SHADOW_HORIZONS)
        or dict(failed_counts)
        != {h: sum(1 for key in reported_failed_keys if key[2] == h) for h in SHADOW_HORIZONS}
    ):
        return "Frozen cohort failure/censored counts do not reconcile."
    hash_payload = {
        "cohort": contract,
        "rows": pairs,
        "failures": failures,
        "provenance_blockers": provenance_blockers,
        "blocked_reasons": blocked_reasons,
        "failed_pairs_by_horizon": failed_counts,
        "failure_only_pairs": failure_only_pairs,
        "failed_pair_identities": failed_pair_identities,
        "snapshot_sha256": cohort["database_sha256"],
    }
    expected_cohort_hash = hashlib.sha256(
        json.dumps(hash_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if cohort.get("cohort_sha256") != expected_cohort_hash:
        return "Frozen cohort content does not match cohort_sha256."
    timestamp_eligible = cohort.get("timestamp_eligible_forecasts")
    legacy_excluded = cohort.get("timestamp_eligible_legacy_excluded")
    versioned_eligible = cohort.get("timestamp_eligible_versioned_forecasts")
    if (
        not _exact_int(timestamp_eligible)
        or not _exact_int(legacy_excluded)
        or not _exact_int(versioned_eligible)
        or legacy_excluded + versioned_eligible != timestamp_eligible
    ):
        return "Frozen cohort legacy/versioned timestamp-eligible counts do not reconcile."
    latest = cohort.get("latest_provenance_complete_origin_at")
    latest_mature = cohort.get("latest_provenance_complete_origin_mature_for_cohort")
    if type(latest_mature) is not bool:
        return "Frozen cohort latest provenance maturity flag is invalid."
    if latest is None:
        if latest_mature:
            return "Frozen cohort cannot mark a missing latest provenance origin as mature."
    else:
        try:
            latest_time = datetime.fromisoformat(str(latest).replace("Z", "+00:00"))
            if latest_time.tzinfo is None or latest_time.utcoffset() != timedelta(0):
                return "Frozen cohort latest provenance origin must be explicit UTC."
            expected_latest_mature = latest_time <= cutoff and latest_time <= as_of - timedelta(
                hours=16
            )
            if latest_mature != expected_latest_mature:
                return "Frozen cohort latest provenance maturity flag does not reconcile."
        except (TypeError, ValueError):
            return "Frozen cohort latest provenance origin is malformed."
    if not computed_ready:
        return (
            str(blocked_reasons[0])
            if blocked_reasons
            else "Frozen cohort is incomplete or blocked."
        )
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
    corpus_path: Path,
    ledger_path: Path | None,
    frozen_cohort_path: Path | None = None,
    protocol_path: Path = PROTOCOL_PATH,
) -> dict[str, Any]:
    """Build a reproducible machine-readable readiness report from evidence files."""
    corpus, corpus_error, corpus_hash = _load(corpus_path)
    ledger, ledger_error, ledger_hash = _load(ledger_path)
    frozen_cohort, frozen_error, frozen_hash = _load(frozen_cohort_path)
    protocol_file_hash: str | None = None
    protocol: Mapping[str, Any] | None = None
    protocol_problem: str | None = None
    try:
        protocol_file_hash = hashlib.sha256(protocol_path.read_bytes()).hexdigest()
        protocol = load_frozen_protocol(protocol_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RuntimeError, ValueError) as exc:
        protocol_problem = f"Frozen scoring protocol is invalid or unavailable: {exc}"
    try:
        protocol_display_path = str(protocol_path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        protocol_display_path = str(protocol_path)
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

    if protocol_problem:
        blockers.append({"code": "frozen_protocol_invalid", "detail": protocol_problem})
    if frozen_cohort_path is None:
        frozen_problem = (
            "Frozen #419 prospective cohort report is required for prospective readiness."
        )
    if frozen_problem:
        blockers.append({"code": "frozen_prospective_cohort_not_ready", "detail": frozen_problem})

    ready = not blockers
    return {
        "schema_version": SCHEMA_VERSION,
        "issue": 411,
        "status": "ready_for_replay" if ready else "blocked",
        "canonical_skill_claim": False,
        "metrics_interpretable_as_canonical": False,
        "metrics": None,
        "metric_status": "not_computed_blocked" if not ready else "replay_not_run",
        "gates": {
            "frozen_protocol": "passed" if protocol_problem is None else "blocked",
            "corpus": "passed" if corpus_ready else "blocked",
            "eligible_mature_production_policy_evidence": "passed"
            if ledger is not None and _ledger_blocker(ledger) is None
            else "blocked",
        },
        "evidence": {
            "frozen_protocol": {
                "path": protocol_display_path,
                "file_sha256": protocol_file_hash,
                "protocol_sha256": protocol.get("protocol_sha256") if protocol else None,
                "protocol_version": protocol.get("protocol_version") if protocol else None,
            },
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
                "origin_cutoff_at": (
                    frozen_cohort.get("cohort", {}).get("origin_cutoff_at")
                    if frozen_cohort is not None
                    and isinstance(frozen_cohort.get("cohort"), Mapping)
                    else None
                ),
                "evaluation_as_of": (
                    frozen_cohort.get("cohort", {}).get("evaluation_as_of")
                    if frozen_cohort is not None
                    and isinstance(frozen_cohort.get("cohort"), Mapping)
                    else None
                ),
                "timestamp_eligible_forecasts": (
                    frozen_cohort.get("timestamp_eligible_forecasts")
                    if frozen_cohort is not None
                    else None
                ),
                "timestamp_eligible_legacy_excluded": (
                    frozen_cohort.get("timestamp_eligible_legacy_excluded")
                    if frozen_cohort is not None
                    else None
                ),
                "confirmatory_forecasts": (
                    frozen_cohort.get("eligible_origins") if frozen_cohort is not None else None
                ),
                "expected_pairs": (
                    frozen_cohort.get("expected_pairs") if frozen_cohort is not None else None
                ),
                "matured_pairs": (
                    frozen_cohort.get("matured_pairs") if frozen_cohort is not None else None
                ),
                "right_censored_forecasts": (
                    frozen_cohort.get("right_censored_forecasts")
                    if frozen_cohort is not None
                    else None
                ),
                "latest_provenance_complete_origin_at": (
                    frozen_cohort.get("latest_provenance_complete_origin_at")
                    if frozen_cohort is not None
                    else None
                ),
                "blocked_reasons": (
                    frozen_cohort.get("blocked_reasons", []) if frozen_cohort is not None else []
                ),
            },
        },
        "frozen_cohort_readiness": {
            "status": "blocked" if frozen_problem else "ready",
            "blocker": frozen_problem,
        },
        "blockers": blockers,
        "evidence_tracks": {
            "historical_canonical": {
                "status": "blocked" if not corpus_ready else "replay_not_run",
                "canonical_corpus_required": True,
                "metrics": None,
            },
            "prospective": {
                "status": "blocked" if frozen_problem else "ready_for_capture_replay",
                "metrics": None,
                "canonical_skill_claim": False,
            },
        },
        "last_verified_prospective_snapshot": {
            "source": "docs/research/FROZEN_SHADOW_COHORT.md sanitized live-artifact verification",
            "snapshot_at": "2026-09-27T11:00:00Z",
            "origin_cutoff_at": "2026-09-26T19:00:00Z",
            "evaluation_as_of": "2026-09-27T11:00:00Z",
            "database_sha256": "b102f14db535264ecc2474a3b338c929abea4c39616f233a5701611c9300f57f",
            "cohort_sha256": "462287eb433a27c16e4f25360d9ee40e5dda2fabdced13e5390859a055f7bbb8",
            "code_sha256": "79a95fc3debd4134ab68cfc55795007a340f15a6eff2ef7212a875a9846c28fc",
            "timestamp_eligible_forecasts": 48,
            "timestamp_eligible_legacy_excluded": 48,
            "confirmatory_forecasts": 0,
            "expected_pairs": 0,
            "matured_pairs": 0,
            "latest_provenance_complete_origin_at": "2026-09-27T11:00:00Z",
            "latest_origin_mature_for_cohort": False,
            "blockers": [
                "versioned_provenance_mismatch",
                "no_confirmatory_versioned_forecasts_in_frozen_cohort",
            ],
            "metrics": None,
        },
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
    parser.add_argument("--protocol", type=Path, default=PROTOCOL_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = build_report(args.corpus_audit, args.ledger, args.frozen_cohort, args.protocol)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
