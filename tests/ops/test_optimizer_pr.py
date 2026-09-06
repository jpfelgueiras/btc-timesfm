from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from btc_timesfm.ops.optimizer_pr import (
    build_candidate_file_edits,
    build_pr_body,
    prepare_pull_request,
)


FORECAST_ENGINE = '''"""test"""
ADAPTIVE_MIN_SAMPLES = 6
ADAPTIVE_FULL_SAMPLES = 24
ADAPTIVE_MAX_BLEND = 0.80
ADAPTIVE_MIN_WEIGHT = 0.03
ADAPTIVE_MAX_WEIGHT = 0.55
ADAPTIVE_MAE_LAMBDA = 2.5
ADAPTIVE_DIRECTION_REWARD = 0.25
PERSISTENCE_FALLBACK_BOOST = 0.12
'''

ADAPTIVE_WEIGHTING = '''"""test"""
DEFAULT_HISTORY_LIMIT = max(ADAPTIVE_MIN_SAMPLES, int(os.getenv("BTC_ADAPTIVE_HISTORY_LIMIT", "200")))
TARGET_INTERVAL_COVERAGE = 0.80
COVERAGE_PENALTY = 0.35
'''


def _candidate() -> dict:
    return {
        "name": "more_samples_before_adapt",
        "parameters": {
            "min_samples": 10,
            "full_samples": 36,
            "max_blend": 0.85,
            "min_weight": 0.05,
            "max_weight": 0.45,
            "mae_lambda": 3.25,
            "direction_reward": 0.35,
            "persistence_boost": 0.2,
            "history_limit": 300,
            "target_interval_coverage": 0.8,
            "coverage_penalty": 0.6,
        },
        "samples": 48,
    }


class OptimizerPrTests(unittest.TestCase):
    def test_candidate_edits_patch_only_explicit_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            forecast_path = root / "src/btc_timesfm/forecasting"
            forecast_path.mkdir(parents=True)
            (forecast_path / "forecast_engine.py").write_text(FORECAST_ENGINE, encoding="utf-8")
            (forecast_path / "adaptive_weighting.py").write_text(
                ADAPTIVE_WEIGHTING, encoding="utf-8"
            )

            edits = build_candidate_file_edits(root, _candidate())
            self.assertEqual(
                {edit.path.name for edit in edits}, {"forecast_engine.py", "adaptive_weighting.py"}
            )
            combined = "\n".join(edit.after for edit in edits)
            self.assertIn("ADAPTIVE_MIN_SAMPLES = 10", combined)
            self.assertIn('BTC_ADAPTIVE_HISTORY_LIMIT", "300"', combined)
            self.assertIn("TARGET_INTERVAL_COVERAGE = 0.8", combined)

    def test_dry_run_prepares_deterministic_pr_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src/btc_timesfm/forecasting"
            source.mkdir(parents=True)
            (source / "forecast_engine.py").write_text(FORECAST_ENGINE, encoding="utf-8")
            (source / "adaptive_weighting.py").write_text(ADAPTIVE_WEIGHTING, encoding="utf-8")

            optimizer_report = {"schema_version": 2}
            promotion_decision = {
                "decision": "review",
                "policy_id": "policy-test",
                "candidate": _candidate(),
                "evidence": {
                    "relative_mae_improvement": 0.1,
                    "horizon_relative_improvement": {"2h": 0.02},
                },
                "checks": {
                    "hard_veto": {"no_severe_drift": True},
                    "review_requirements": {"enough_samples": True},
                },
            }
            champion_report = {
                "comparison_id": "champion-challenger-abc",
                "champion": {"manifest": {"configuration_id": "cfg-champion"}},
                "challenger": {
                    "manifest": {"configuration_id": "cfg-challenger", "name": _candidate()["name"]}
                },
            }

            result = prepare_pull_request(
                root,
                optimizer_report,
                promotion_decision,
                champion_report,
                dry_run=True,
            )
            self.assertTrue(result["branch"].startswith("optimizer-pr/"))
            self.assertTrue(any(path.endswith("forecast_engine.py") for path in result["edits"]))
            self.assertIn("Safe optimizer parameter change", result["body"])
            self.assertIn("cfg-challenger", result["body"])

    def test_pr_body_mentions_evidence_and_manifests(self) -> None:
        body = build_pr_body(
            {"schema_version": 2},
            {
                "policy_id": "policy-test",
                "candidate": _candidate(),
                "evidence": {
                    "relative_mae_improvement": 0.1,
                    "horizon_relative_improvement": {"2h": 0.02},
                },
                "checks": {"hard_veto": {}, "review_requirements": {}},
            },
            {
                "comparison_id": "champion-challenger-abc",
                "champion": {"manifest": {"configuration_id": "cfg-champion"}},
                "challenger": {"manifest": {"configuration_id": "cfg-challenger"}},
            },
        )
        self.assertIn("policy-test", body)
        self.assertIn("cfg-champion", body)
        self.assertIn("2h", body)


if __name__ == "__main__":
    unittest.main()
