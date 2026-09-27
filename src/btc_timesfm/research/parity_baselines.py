"""Capture and exact-key replay helpers for registered production-parity baselines."""

from __future__ import annotations

import hashlib
import argparse
import json
import math
import numpy as np
from datetime import timedelta
from pathlib import Path
from typing import Sequence
from typing import Any, Iterable, Mapping

from btc_timesfm.forecasting.benchmarks import BENCHMARK_NAMES, benchmark_forecasts
from btc_timesfm.forecasting.forecast_engine import MarketData
from btc_timesfm.research.frozen_scoring_protocol import load_frozen_protocol, parse_utc


def market_window_sha256(data: MarketData) -> str:
    """Fingerprint exactly the OHLCV window handed to the production and baselines."""
    digest = hashlib.sha256()
    timestamps = [int(value) for value in data.timestamps]
    digest.update(np.asarray(timestamps, dtype="<i8").tobytes())
    for name in ("opens", "highs", "lows", "closes", "volumes"):
        values = np.asarray(getattr(data, name), dtype="<f8")
        digest.update(name.encode("ascii"))
        digest.update(values.tobytes())
    return digest.hexdigest()


def build_baseline_attempts(
    data: MarketData,
    *,
    origin_at: str,
    configuration_id: str,
    data_lineage_id: str,
    market_source: str,
    market_pair: str,
    policy_id: str,
    production_policy_sha256: str,
    code_sha: str,
    model_identity: Mapping[str, Any],
    capture_id: str,
) -> list[dict[str, Any]]:
    """Create a complete attempt ledger; missing/invalid baseline outputs become failures."""
    origin = parse_utc(origin_at)
    if not all(
        (
            configuration_id,
            data_lineage_id,
            market_source,
            market_pair,
            policy_id,
            production_policy_sha256,
            code_sha,
            capture_id,
        )
    ):
        raise ValueError("baseline capture requires complete production lineage")
    if not all(
        isinstance(model_identity.get(key), str) and model_identity[key]
        for key in ("id", "revision", "package", "package_version")
    ):
        raise ValueError("baseline capture requires complete model identity")
    window_hash = market_window_sha256(data)
    expected_origin = int(origin.timestamp())
    preflight_error = None
    if any(timestamp > expected_origin for timestamp in data.timestamps):
        preflight_error = "baseline MarketData contains observations after the production origin"
    elif not data.timestamps or int(data.timestamps[-1]) != expected_origin:
        preflight_error = "baseline MarketData must end exactly at the production origin"
    elif any(right <= left for left, right in zip(data.timestamps, data.timestamps[1:])):
        preflight_error = "baseline MarketData timestamps must be strictly increasing"
    forecasts: Mapping[str, Any] = {}
    suite_error: str | None = preflight_error
    if suite_error is None:
        try:
            forecasts = benchmark_forecasts(data)
        except Exception as exc:  # preserve failures from unexpected baseline errors
            suite_error = f"{type(exc).__name__}: {exc}"
    rows: list[dict[str, Any]] = []
    for benchmark in BENCHMARK_NAMES:
        predictions = forecasts.get(benchmark)
        for horizon in (2, 4, 8, 16):
            target = origin + timedelta(hours=horizon)
            item = predictions.get(f"{horizon}h") if isinstance(predictions, Mapping) else None
            try:
                price = float(item["price_usd"]) if isinstance(item, Mapping) else math.nan
                if not math.isfinite(price) or price <= 0:
                    raise ValueError("baseline prediction must be finite and positive")
                status, final = "scored", {"price_usd": price}
                failure_type = failure_message = None
            except (KeyError, TypeError, ValueError) as exc:
                status, final = "failed", None
                failure_type = (
                    "PointInTimeValidationError" if preflight_error else type(exc).__name__
                )
                failure_message = suite_error or str(exc)
            rows.append(
                {
                    "attempt_id": f"{capture_id}:{benchmark}:{horizon}h",
                    "configuration_id": configuration_id,
                    "origin_at": origin.isoformat(),
                    "target_at": target.isoformat(),
                    "horizon": f"{horizon}h",
                    "benchmark_id": benchmark,
                    "status": status,
                    "raw_prediction": final,
                    "final_prediction": final,
                    "market_pair": market_pair,
                    "market_source": market_source,
                    "data_lineage_id": data_lineage_id,
                    "source_window_sha256": window_hash,
                    "policy_id": policy_id,
                    "production_policy_sha256": production_policy_sha256,
                    "code_sha": code_sha,
                    "model_identity": dict(model_identity),
                    "failure_type": failure_type,
                    "failure_message": failure_message,
                }
            )
    return rows


def production_attempts_from_cohort(cohort: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Expose production champion point predictions under exact frozen pair keys."""
    attempts = []
    for pair in cohort.get("pairs", []):
        prediction = pair.get("production_final_prediction")
        raw = pair.get("production_raw_prediction")
        attempts.append(
            {
                **dict(pair),
                "status": "scored"
                if pair.get("matured") is True and isinstance(prediction, Mapping)
                else "failed",
                "raw_prediction": raw,
                "final_prediction": prediction,
                "actual_source_identity": pair.get("actual_source_identity"),
            }
        )
    return attempts


def replay_frozen_cohort(
    cohort: Mapping[str, Any],
    production_attempts: Iterable[Mapping[str, Any]],
    baseline_attempts: Iterable[Mapping[str, Any]],
    *,
    protocol_path: Path | None = None,
) -> dict[str, Any]:
    """Exact-key readiness/replay; metrics remain null until every registered gate passes."""
    from btc_timesfm.research.production_parity_replay import _frozen_cohort_blocker

    protocol = (
        load_frozen_protocol() if protocol_path is None else load_frozen_protocol(protocol_path)
    )
    cohort_problem = _frozen_cohort_blocker(cohort)
    expected_pairs = list(cohort.get("pairs", [])) if isinstance(cohort, Mapping) else []
    production_rows = list(production_attempts)
    baseline_rows = list(baseline_attempts)
    baseline_index: dict[tuple[str, str, str, str, str], list[Mapping[str, Any]]] = {}
    for row in baseline_rows:
        key = (
            str(row.get("configuration_id")),
            str(row.get("origin_at")),
            str(row.get("target_at")),
            str(row.get("horizon")),
            str(row.get("benchmark_id")),
        )
        baseline_index.setdefault(key, []).append(row)
    production_index: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = {}
    for row in production_rows:
        key = (
            str(row.get("configuration_id")),
            str(row.get("origin_at")),
            str(row.get("target_at")),
            str(row.get("horizon")),
        )
        production_index.setdefault(key, []).append(row)
    output_rows: list[dict[str, Any]] = []
    blockers: list[str] = []
    used_attempt_ids: set[int] = set()
    for pair in expected_pairs:
        key = (
            str(pair.get("configuration_id")),
            str(pair.get("origin_at")),
            str(pair.get("target_at")),
            str(pair.get("horizon")),
        )
        row: dict[str, Any] = {"pair_key": list(key), "cohort_pair": dict(pair), "models": {}}
        for model in ("production", *BENCHMARK_NAMES):
            attempts = (
                production_index.get(key, [])
                if model == "production"
                else baseline_index.get((*key, model), [])
            )
            if not attempts:
                row["models"][model] = {"status": "missing", "attempts": []}
                blockers.append(f"missing_{model}_attempt")
                continue
            row["models"][model] = {
                "status": "scored"
                if len(attempts) == 1 and attempts[0].get("status") == "scored"
                else "failed",
                "attempts": [dict(item) for item in attempts],
            }
            used_attempt_ids.update(id(item) for item in attempts)
            if len(attempts) != 1 or attempts[0].get("status") != "scored":
                blockers.append(f"ineligible_{model}_attempt")
            for item in attempts:
                if item.get("data_lineage_id") != pair.get("data_lineage_id"):
                    blockers.append(f"{model}_data_lineage_mismatch")
                if item.get("target_at") != pair.get("target_at"):
                    blockers.append(f"{model}_target_mismatch")
                if item.get("source_window_sha256") != pair.get("source_window_sha256"):
                    blockers.append(f"{model}_source_window_mismatch")
                if item.get("policy_id") != pair.get("policy_id"):
                    blockers.append(f"{model}_policy_mismatch")
                if item.get("code_sha") != pair.get("code_sha"):
                    blockers.append(f"{model}_code_mismatch")
                if item.get("model_identity") != pair.get("model_identity"):
                    blockers.append(f"{model}_model_identity_mismatch")
                if item.get("market_source") != pair.get("market_source"):
                    blockers.append(f"{model}_market_source_mismatch")
                if item.get("market_pair") != pair.get("market_pair"):
                    blockers.append(f"{model}_market_pair_mismatch")
        prod_rows = row["models"].get("production", {}).get("attempts", [])
        if len(prod_rows) == 1:
            production_identity = prod_rows[0]
            for model in BENCHMARK_NAMES:
                for attempt in row["models"][model]["attempts"]:
                    for field in (
                        "market_pair",
                        "market_source",
                        "data_lineage_id",
                        "source_window_sha256",
                        "policy_id",
                        "production_policy_sha256",
                        "code_sha",
                        "model_identity",
                    ):
                        if attempt.get(field) != production_identity.get(field):
                            blockers.append(f"{model}_{field}_mismatch")
        actual_source_identity = pair.get("actual_source_identity")
        if (
            pair.get("matured") is not True
            or not isinstance(pair.get("actual_value"), (int, float))
            or isinstance(pair.get("actual_value"), bool)
            or not math.isfinite(float(pair.get("actual_value")))
            or not isinstance(actual_source_identity, str)
            or not actual_source_identity
        ):
            blockers.append("actual_source_identity_mismatch")
        for model_rows in row["models"].values():
            for attempt in model_rows["attempts"]:
                attempt["actual_value"] = pair.get("actual_value")
                attempt["actual_source_identity"] = actual_source_identity
        output_rows.append(row)
    if not expected_pairs:
        blockers.append("no_frozen_eligible_pairs")
    if cohort_problem:
        blockers.append("frozen_cohort_blocked")
    blockers.append("training_only_strongest_baseline_selection_unavailable")
    if any(
        "actual_value" not in pair or "actual_source_identity" not in pair
        for pair in expected_pairs
    ):
        blockers.append("frozen_cohort_missing_actual_source_identity")
    all_attempts = [*production_rows, *baseline_rows]
    unmatched_attempts = [dict(item) for item in all_attempts if id(item) not in used_attempt_ids]
    if unmatched_attempts:
        blockers.append("unmatched_attempt_rows_present")
    unique_blockers = sorted(set(blockers))
    scored = bool(expected_pairs) and not unique_blockers
    return {
        "schema_version": 1,
        "status": "ready_for_scoring" if scored else "blocked",
        "protocol_sha256": protocol["protocol_sha256"],
        "cohort_sha256": cohort.get("cohort_sha256") if isinstance(cohort, Mapping) else None,
        "eligible_pairs": len(expected_pairs),
        "attempt_rows": output_rows,
        "unmatched_attempts": unmatched_attempts,
        "blockers": unique_blockers,
        "metrics": None,
        "metrics_computed": False,
        "strongest_simple_baseline": None,
        "strongest_baseline_blocker": "training-only nested purged selection is not available",
        "canonical_skill_claim": False,
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", required=True, type=Path, help="immutable #419 cohort JSON")
    parser.add_argument("--baseline-attempts", required=True, type=Path)
    parser.add_argument("--production-attempts", type=Path)
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    cohort_bytes = args.cohort.read_bytes()
    baseline_bytes = args.baseline_attempts.read_bytes()
    cohort = json.loads(cohort_bytes.decode("utf-8"))
    baseline_payload = json.loads(baseline_bytes.decode("utf-8"))
    if isinstance(baseline_payload, Mapping):
        baseline_attempts = baseline_payload.get("attempts")
    else:
        baseline_attempts = baseline_payload
    if not isinstance(cohort, Mapping) or not isinstance(baseline_attempts, list):
        raise ValueError("cohort must be a JSON object and baseline attempts a list or export")
    if args.production_attempts is None:
        production_attempts = production_attempts_from_cohort(cohort)
        production_bytes = cohort_bytes
    else:
        production_bytes = args.production_attempts.read_bytes()
        production_attempts = json.loads(production_bytes.decode("utf-8"))
        if not isinstance(production_attempts, list):
            raise ValueError("production attempts must be a JSON list")
    report = replay_frozen_cohort(
        cohort,
        production_attempts,
        baseline_attempts,
        protocol_path=args.protocol,
    )
    report["input_artifacts"] = {
        "cohort": {"path": str(args.cohort), "sha256": hashlib.sha256(cohort_bytes).hexdigest()},
        "baseline_attempts": {
            "path": str(args.baseline_attempts),
            "sha256": hashlib.sha256(baseline_bytes).hexdigest(),
        },
        "production_attempts": {
            "path": str(args.production_attempts) if args.production_attempts else str(args.cohort),
            "sha256": hashlib.sha256(production_bytes).hexdigest(),
        },
    }
    rendered = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
