#!/usr/bin/env python3
"""Tests for champion-vs-challenger reporting."""

from __future__ import annotations

import copy
import unittest

from btc_timesfm.research.champion_challenger import (
    build_report,
    configuration_manifest,
    render_summary,
)

HORIZONS = ("2h", "4h", "8h", "16h")


def _candidate(name: str, mae: float, coverage: float) -> dict:
    origins = [
        "2026-01-01T00:00:00+00:00",
        "2026-01-02T00:00:00+00:00",
        "2026-01-03T00:00:00+00:00",
    ]
    by_horizon = {
        horizon: {
            "samples": 3,
            "mae_pct": mae,
            "mean_signed_error_pct": mae / 10,
            "direction_accuracy": 2 / 3,
            "interval_coverage": coverage,
            "average_interval_width_pct": 2.5,
        }
        for horizon in HORIZONS
    }
    return {
        "name": name,
        "parameters": {"history_limit": 200 if name == "production" else 300},
        "samples": 3,
        "objective_mae_pct": mae,
        "mean_direction_accuracy": 2 / 3,
        "by_horizon": by_horizon,
        "by_regime": {"range": copy.deepcopy(by_horizon)},
        "persistence_by_horizon": {horizon: {"samples": 3, "mae_pct": 1.2} for horizon in HORIZONS},
        "folds": [
            {"fold": 1, "mae_pct": mae},
            {"fold": 2, "mae_pct": mae + 0.01},
            {"fold": 3, "mae_pct": mae - 0.01},
        ],
        "paired_metrics": {
            "origins": origins,
            "mae_pct": [mae, mae, mae],
            "direction_accuracy": [1.0, 0.0, 1.0],
            "by_horizon": {horizon: [mae, mae, mae] for horizon in HORIZONS},
            "persistence_mae_pct": [1.2, 1.2, 1.2],
        },
    }


def _optimizer_report() -> dict:
    production = _candidate("production", 1.0, 0.80)
    challenger = _candidate("longer_history", 0.9, 0.82)
    return {
        "schema_version": 3,
        "generated_at": "2026-01-04T00:00:00+00:00",
        "data_source": "synthetic",
        "tested_period": {"days": 30, "samples": 3},
        "candidates": [production, challenger],
        "comparison": {
            "relative_mae_improvement": 0.1,
            "significance": {
                "candidate_vs_production": {
                    "mae_pct": {"conclusion": "candidate_better"},
                },
                "candidate_vs_persistence": {
                    "mae_pct": {"conclusion": "candidate_better"},
                },
            },
        },
    }


def _promotion_decision() -> dict:
    return {
        "policy_version": 1,
        "policy_id": "promotion-policy-test",
        "decision": "review",
        "reasons": ["all_promotion_guardrails_passed"],
        "candidate": {"name": "longer_history"},
        "checks": {"hard_veto": {}, "review_requirements": {}},
        "evidence": {"relative_mae_improvement": 0.1},
        "production_health": {"overall_health": "healthy"},
    }


class ChampionChallengerTests(unittest.TestCase):
    def test_report_is_reproducible_and_paired(self) -> None:
        report_a = build_report(
            _optimizer_report(),
            _promotion_decision(),
            generated_at="2026-01-04T01:00:00+00:00",
        )
        report_b = build_report(
            _optimizer_report(),
            _promotion_decision(),
            generated_at="2026-01-04T01:00:00+00:00",
        )
        self.assertEqual(report_a, report_b)
        self.assertTrue(report_a["pairing"]["identical_origins"])
        self.assertEqual(report_a["pairing"]["origin_count"], 3)
        policy = report_a["policy_recommendation"]
        self.assertEqual(policy["decision"], "review")
        self.assertEqual(
            policy["production_health"]["overall_health"],
            "healthy",
        )
        significance = report_a["statistical_evidence"]
        self.assertEqual(
            significance["candidate_vs_production"]["mae_pct"]["conclusion"],
            "candidate_better",
        )

    def test_rejects_non_identical_origins(self) -> None:
        optimizer_report = _optimizer_report()
        optimizer_report["candidates"][1]["paired_metrics"]["origins"][1] = (
            "2026-01-02T01:00:00+00:00"
        )
        with self.assertRaisesRegex(ValueError, "identical origins"):
            build_report(optimizer_report, _promotion_decision())

    def test_configuration_identity_changes_with_parameters(self) -> None:
        candidate = _candidate("candidate", 0.9, 0.8)
        first = configuration_manifest(candidate, "challenger")
        second = configuration_manifest(candidate, "challenger")
        self.assertEqual(first["configuration_id"], second["configuration_id"])
        candidate["parameters"]["history_limit"] = 400
        third = configuration_manifest(candidate, "challenger")
        self.assertNotEqual(first["configuration_id"], third["configuration_id"])

    def test_summary_includes_required_metrics(self) -> None:
        report = build_report(_optimizer_report(), _promotion_decision())
        summary = render_summary(report)
        self.assertIn("Champion vs challenger", summary)
        self.assertIn("Champion MAE", summary)
        self.assertIn("Champion bias", summary)
        self.assertIn("Champion direction", summary)
        self.assertIn("Champion coverage", summary)
        self.assertIn("Persistence MAE", summary)
        self.assertIn("candidate_better", summary)
        self.assertIn("Policy recommendation: **review**", summary)

    def test_policy_candidate_must_match_selected_challenger(self) -> None:
        decision = _promotion_decision()
        decision["candidate"]["name"] = "wrong_candidate"
        with self.assertRaisesRegex(ValueError, "does not match challenger"):
            build_report(_optimizer_report(), decision)


if __name__ == "__main__":
    unittest.main()
