from __future__ import annotations

import unittest

from btc_timesfm.research.context_comparison import (
    assess_candidate,
    build_report,
    candidate_catalog,
    comparison_designs,
    validate_corpus_manifest,
    validate_origin_metadata,
)
from btc_timesfm.research.canonical_benchmark import (
    TARGET_END,
    TARGET_START,
    TARGET_END_TS,
    TARGET_START_TS,
    WARMUP_START_TS,
)
from datetime import datetime, timezone


class ContextComparisonTests(unittest.TestCase):
    def test_catalog_is_bounded_and_production_identity_is_pinned(self) -> None:
        production = candidate_catalog()[0]
        self.assertEqual(production.supported_contexts, (168, 336, 512))
        self.assertNotIn(64, production.supported_contexts)
        self.assertNotIn(1024, production.supported_contexts)
        self.assertEqual(production.revision, "43046b85ec22d584a13f8098c2ed39c889e129c2")
        self.assertFalse(candidate_catalog()[1].authorized)

    def test_physical_lookback_counts_closes_not_only_returns(self) -> None:
        result = assess_candidate(candidate_catalog()[0], available_returns=336)
        self.assertEqual(result["supported_contexts"], [168, 336])
        self.assertEqual(result["physical_lookback_candles"]["336"], 337)
        self.assertFalse(result["eligible"])

    @staticmethod
    def valid_manifest() -> dict:
        target_hours = (TARGET_END_TS - TARGET_START_TS) // 3600
        return {
            "status": "ready_for_replay",
            "source_sha256": "a" * 64,
            "venue": "Kraken",
            "pair": "XBT/USD",
            "target_period": {
                "start_inclusive": TARGET_START.isoformat(),
                "end_exclusive": TARGET_END.isoformat(),
                "expected_hours": target_hours,
                "observed_hours": target_hours,
                "missing_hours": 0,
            },
            "warmup_period": {
                "start_inclusive": datetime.fromtimestamp(
                    WARMUP_START_TS, timezone.utc
                ).isoformat(),
                "end_exclusive": TARGET_START.isoformat(),
                "expected_hours": 180 * 24,
                "observed_hours": 180 * 24,
                "missing_hours": 0,
            },
            "invalid_rows": 0,
            "gaps": 0,
            "duplicates": 0,
        }

    def test_audit_manifest_requires_ready_provenance_and_exact_coverage(self) -> None:
        manifest = self.valid_manifest()
        self.assertTrue(validate_corpus_manifest(manifest)["eligible"])
        forged = {**manifest, "source_sha256": "not-a-hash"}
        self.assertFalse(validate_corpus_manifest(forged)["eligible"])
        missing_coverage = {
            **manifest,
            "target_period": {**manifest["target_period"], "missing_hours": 1},
        }
        self.assertFalse(validate_corpus_manifest(missing_coverage)["eligible"])

    def test_origin_pairing_is_bound_to_audited_source_and_target(self) -> None:
        manifest = self.valid_manifest()
        record = {
            "source_sha256": manifest["source_sha256"],
            "venue": manifest["venue"],
            "pair": manifest["pair"],
            "target_start": TARGET_START.isoformat(),
            "target_end": TARGET_END.isoformat(),
            "origins": ["2025-01-01T00:00:00+00:00", "2025-01-02T00:00:00+00:00"],
        }
        paired = validate_origin_metadata({"a": record, "b": dict(record)}, manifest)
        self.assertTrue(paired["paired"])
        forged = {**record, "source_sha256": "b" * 64}
        self.assertFalse(validate_origin_metadata({"a": forged}, manifest)["paired"])
        self.assertFalse(validate_origin_metadata(None, manifest)["paired"])
        mismatched = {**record, "origins": ["2025-01-02T00:00:00+00:00"]}
        self.assertFalse(
            validate_origin_metadata({"a": record, "b": mismatched}, manifest)["paired"]
        )
        out_of_range = {**record, "origins": ["2022-12-31T23:00:00+00:00"]}
        self.assertFalse(validate_origin_metadata({"a": out_of_range}, manifest)["paired"])

    def test_report_rejects_boolean_gate_and_never_becomes_ready_from_strings(self) -> None:
        with self.assertRaises(TypeError):
            build_report(corpus_eligible=True)  # type: ignore[call-arg]
        manifest = self.valid_manifest()
        report = build_report(audit_manifest=manifest)
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["origin_pairing"]["paired"])
        self.assertFalse(report["forecast_skill_claimed"])

    def test_default_report_is_explicitly_blocked_without_skill_claim(self) -> None:
        report = build_report()
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["inference_performed"])
        self.assertFalse(report["forecast_skill_claimed"])
        self.assertTrue(report["blocked_reasons"])

    def test_different_context_lookbacks_are_policy_comparisons_not_controlled_steps(self) -> None:
        default = comparison_designs()
        self.assertEqual(default["controlled_step_count_comparison"]["status"], "blocked")
        self.assertEqual(
            default["policy_context_comparison"]["physical_lookback_hours"]["336"], 336
        )
        verified = comparison_designs(
            {
                "status": "verified",
                "model_id": "google/timesfm-3.0-pytorch",
                "revision": "43046b85ec22d584a13f8098c2ed39c889e129c2",
                "api": "TimesFM3Evaluator.predict_batch",
                "artifact_sha256": "c" * 64,
                "supported_contexts": [168, 336],
                "supported_frequencies": ["hourly", "2-hourly"],
                "context_lookbacks_hours": {"168": 336, "336": 336},
            }
        )
        self.assertEqual(verified["controlled_step_count_comparison"]["status"], "eligible")
        forged = comparison_designs({"verified": True, "supported_contexts": [168, 336]})
        self.assertEqual(forged["controlled_step_count_comparison"]["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
