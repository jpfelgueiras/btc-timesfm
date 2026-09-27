"""Tests for fail-closed production parity readiness reports."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from btc_timesfm.research.production_parity_replay import (
    SHADOW_HORIZONS,
    _frozen_cohort_blocker,
    build_report,
)


def corpus_audit(**updates: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "status": "ready_for_replay",
        "eligible_for_skill_comparison": False,
        "source_file": "licensed-source.csv",
        "source_sha256": "a" * 64,
        "venue": "Example Exchange",
        "pair": "BTC/USD",
        "target_period": {"expected_hours": 32136, "observed_hours": 32136, "missing_hours": 0},
        "target_period_observations": 32136,
        "warmup_period": {"expected_hours": 4320, "observed_hours": 4320, "missing_hours": 0},
        "gaps": 0,
        "duplicates": 0,
        "invalid_rows": 0,
        "errors": [],
        "vintage_column_present": True,
        "revision_column_present": True,
    }
    report.update(updates)
    return report


def shadow_status(**evidence_updates: Any) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "schema_version": 6,
        "forecast_schema_version": 1,
        "confirmatory_forecasts": 2,
        "expected_pairs": 8,
        "matured_pairs": 8,
        "maturity_fraction": 1.0,
        "missing_pairs_by_horizon": {"2h": 0, "4h": 0, "8h": 0, "16h": 0},
        "failures": 0,
        "failed_attempt_pairs": 0,
        "forecast_versions": {"1": 2},
    }
    evidence.update(evidence_updates)
    return {
        "schema_version": 1,
        "policy_id": "shadow-policy-v1",
        "policy_parity_attested": True,
        "champion": {"configuration_id": "champion-config", "name": "Production champion"},
        "evidence": evidence,
    }


def frozen_cohort_report() -> dict[str, Any]:
    contract = {
        "origin_cutoff_at": "2026-01-01T00:00:00+00:00",
        "evaluation_as_of": "2026-01-01T16:00:00+00:00",
        "horizons": list(SHADOW_HORIZONS),
        "maximum_horizon_hours": 16,
        "rule": "origin <= cutoff and origin + 16h <= evaluation_as_of; exact UTC targets",
    }
    pairs = [
        {
            "configuration_id": "cfg",
            "origin_at": "2026-01-01T00:00:00+00:00",
            "target_at": f"2026-01-01T{int(horizon[:-1]):02d}:00:00+00:00",
            "horizon": horizon,
            "policy_id": "policy",
            "data_lineage_id": "data",
            "forecast_sha256": "forecast",
            "model_identity": {
                "id": "model",
                "revision": "rev",
                "package": "pkg",
                "package_version": "1",
            },
            "matured": True,
        }
        for horizon in SHADOW_HORIZONS
    ]
    database_sha256 = "a" * 64
    failures: list[dict[str, Any]] = []
    failed_counts = dict.fromkeys(SHADOW_HORIZONS, 0)
    failure_only: list[dict[str, Any]] = []
    failed_identities: list[dict[str, Any]] = []
    payload = {
        "cohort": contract,
        "rows": pairs,
        "failures": failures,
        "failed_pairs_by_horizon": failed_counts,
        "failure_only_pairs": failure_only,
        "failed_pair_identities": failed_identities,
        "snapshot_sha256": database_sha256,
    }
    return {
        "schema_version": 1,
        "cohort": contract,
        "cohort_sha256": hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest(),
        "database_sha256": database_sha256,
        "code_sha256": "b" * 64,
        "eligible_origins": 1,
        "expected_pairs": 4,
        "matured_pairs": 4,
        "missing_pairs_by_horizon": dict.fromkeys(SHADOW_HORIZONS, 0),
        "pairs_by_horizon": {
            horizon: {"expected": 1, "matured": 1, "missing": 0} for horizon in SHADOW_HORIZONS
        },
        "failures": failures,
        "failed_pairs_by_horizon": failed_counts,
        "failure_only_pairs": failure_only,
        "failed_pair_identities": failed_identities,
        "pairs": pairs,
        "lineage_identities": [["cfg", "policy", "data", "forecast", "model", "rev", "pkg", "1"]],
        "right_censored_forecasts": 0,
        "post_cutoff_forecasts": 0,
        "right_censored_pairs_by_horizon": dict.fromkeys(SHADOW_HORIZONS, 0),
        "ready": True,
    }


class ProductionParityReplayTests(unittest.TestCase):
    def test_frozen_cohort_readiness_is_fail_closed(self) -> None:
        cohort = frozen_cohort_report()
        self.assertIsNone(_frozen_cohort_blocker(cohort))
        cohort["failures"] = [{"stage": "forecast"}]
        self.assertIsNotNone(_frozen_cohort_blocker(cohort))

    def test_frozen_cohort_report_hash_and_count_tampering_are_rejected(self) -> None:
        cohort = frozen_cohort_report()
        cohort["pairs"][0]["target_at"] = "2026-01-01T03:00:00+00:00"
        self.assertIsNotNone(_frozen_cohort_blocker(cohort))
        cohort = frozen_cohort_report()
        cohort["pairs_by_horizon"]["2h"]["missing"] = 1
        self.assertIsNotNone(_frozen_cohort_blocker(cohort))

    def test_frozen_cohort_readiness_is_reported_separately_from_historical_corpus(self) -> None:
        cohort = frozen_cohort_report()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus_path = root / "corpus.json"
            cohort_path = root / "cohort.json"
            corpus_path.write_text(json.dumps({"status": "blocked"}), encoding="utf-8")
            cohort_path.write_text(json.dumps(cohort), encoding="utf-8")
            report = build_report(corpus_path, None, cohort_path)
        self.assertEqual(report["frozen_cohort_readiness"]["status"], "ready")
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["evidence"]["frozen_prospective_cohort"]["canonical_skill_claim"])

    def _report(
        self, corpus: dict[str, Any], ledger: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus_path = root / "corpus.json"
            corpus_path.write_text(json.dumps(corpus), encoding="utf-8")
            ledger_path = None
            if ledger is not None:
                ledger_path = root / "ledger.json"
                ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            return build_report(corpus_path, ledger_path)

    def test_current_blocked_issue_397_audit_stays_blocked(self) -> None:
        current = Path("docs/research/ISSUE_397_CANONICAL_BENCHMARK_AUDIT.json")
        corpus = json.loads(current.read_text(encoding="utf-8"))
        report = self._report(corpus)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["gates"]["corpus"], "blocked")
        self.assertIsNone(report["metrics"])
        self.assertFalse(report["canonical_skill_claim"])

    def test_structurally_complete_corpus_can_pass_without_skill_eligibility_flag(self) -> None:
        report = self._report(corpus_audit())
        self.assertEqual(report["gates"]["corpus"], "passed")
        self.assertFalse(report["canonical_skill_claim"])
        self.assertIsNone(report["metrics"])

    def test_forged_status_or_skill_boolean_does_not_pass_corpus_gate(self) -> None:
        corpus = corpus_audit(status="blocked", eligible_for_skill_comparison=True)
        report = self._report(corpus)
        self.assertEqual(report["gates"]["corpus"], "blocked")

    def test_corpus_counts_require_exact_integers(self) -> None:
        for field, bad_value in (
            ("target_period_observations", 32136.0),
            ("target_period_observations", True),
            ("gaps", 0.0),
            ("duplicates", False),
            ("invalid_rows", 0.0),
        ):
            with self.subTest(field=field, bad_value=bad_value):
                report = self._report(corpus_audit(**{field: bad_value}))
                self.assertEqual(report["gates"]["corpus"], "blocked")

    def test_nonzero_invalid_rows_block_corpus(self) -> None:
        report = self._report(corpus_audit(invalid_rows=1))
        self.assertEqual(report["gates"]["corpus"], "blocked")

    def test_unhashable_pair_values_block_corpus(self) -> None:
        for pair in ([], {}):
            with self.subTest(pair=pair):
                report = self._report(corpus_audit(pair=pair))
                self.assertEqual(report["gates"]["corpus"], "blocked")

    def test_invalid_utf8_is_reported_as_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corpus.json"
            path.write_bytes(b"{\xff}")
            report = build_report(path, None)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["blockers"][0]["code"], "corpus_audit_unreadable")
        self.assertIsNone(report["evidence"]["corpus_audit"]["sha256"])

    def test_hash_matches_exact_bytes_parsed_even_if_file_changes_after_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corpus.json"
            original = json.dumps(corpus_audit()).encode("utf-8")
            path.write_bytes(original)
            read_bytes = Path.read_bytes

            def read_then_replace(instance: Path) -> bytes:
                content = read_bytes(instance)
                instance.write_bytes(b'{"status":"changed"}')
                return content

            with patch.object(Path, "read_bytes", read_then_replace):
                report = build_report(path, None)

        self.assertEqual(report["gates"]["corpus"], "passed")
        self.assertEqual(
            report["evidence"]["corpus_audit"]["sha256"], hashlib.sha256(original).hexdigest()
        )

    def test_actual_shadow_status_contract_passes_only_when_fully_eligible(self) -> None:
        report = self._report(corpus_audit(), shadow_status())
        self.assertEqual(report["status"], "ready_for_replay")
        self.assertEqual(report["gates"]["eligible_mature_production_policy_evidence"], "passed")
        self.assertFalse(report["canonical_skill_claim"])
        self.assertFalse(report["metrics_interpretable_as_canonical"])
        self.assertIsNone(report["metrics"])

    def test_synthetic_status_label_cannot_qualify_ledger(self) -> None:
        report = self._report(corpus_audit(), {"status": "eligible"})
        self.assertEqual(report["gates"]["eligible_mature_production_policy_evidence"], "blocked")

    def test_ledger_requires_attestation_and_reconciled_mature_counts(self) -> None:
        unattested = shadow_status()
        unattested["policy_parity_attested"] = False
        wrong_schema = shadow_status()
        wrong_schema["evidence"]["schema_version"] = 99
        under_matured = shadow_status()
        under_matured["evidence"]["matured_pairs"] = 7
        wrong_missing = shadow_status()
        wrong_missing["evidence"]["missing_pairs_by_horizon"] = {"2h": 0}
        empty = shadow_status()
        empty["evidence"]["confirmatory_forecasts"] = 0
        cases = (unattested, wrong_schema, under_matured, wrong_missing, empty)
        for ledger in cases:
            with self.subTest(ledger=ledger):
                report = self._report(corpus_audit(), ledger)
                self.assertEqual(
                    report["gates"]["eligible_mature_production_policy_evidence"], "blocked"
                )
                self.assertFalse(report["canonical_skill_claim"])
                self.assertIsNone(report["metrics"])


if __name__ == "__main__":
    unittest.main()
