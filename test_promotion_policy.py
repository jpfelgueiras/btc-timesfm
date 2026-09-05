#!/usr/bin/env python3
"""Tests for the conservative optimizer promotion policy."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from promotion_policy import (
    PromotionPolicy,
    build_decision_report,
    evaluate_promotion,
    policy_identity,
)


HORIZONS = ("2h", "4h", "8h", "16h")


def _candidate(
    name: str,
    *,
    mae: float,
    direction: float = 0.60,
    samples: int = 48,
    horizon_maes: dict[str, float] | None = None,
    regime_maes: dict[str, float] | None = None,
    persistence_mae: float = 1.20,
    fold_maes: tuple[float, ...] = (1.0, 1.0, 1.0),
) -> dict:
    horizons = horizon_maes or {horizon: mae for horizon in HORIZONS}
    regime_values = regime_maes or {horizon: mae for horizon in HORIZONS}
    return {
        "name": name,
        "parameters": {"name": name, "max_blend": 0.8},
        "samples": samples,
        "objective_mae_pct": mae,
        "mean_direction_accuracy": direction,
        "by_horizon": {
            horizon: {
                "samples": samples,
                "mae_pct": horizons[horizon],
                "direction_accuracy": direction,
            }
            for horizon in HORIZONS
        },
        "by_regime": {
            "range": {
                horizon: {
                    "samples": 12,
                    "mae_pct": regime_values[horizon],
                    "direction_accuracy": direction,
                }
                for horizon in HORIZONS
            }
        },
        "persistence_by_horizon": {
            horizon: {"samples": samples, "mae_pct": persistence_mae} for horizon in HORIZONS
        },
        "folds": [
            {"fold": index, "mae_pct": fold_mae}
            for index, fold_mae in enumerate(fold_maes, start=1)
        ],
    }


def _report(
    challenger: dict,
    *,
    production_conclusion: str = "candidate_better",
    persistence_conclusion: str = "candidate_better",
) -> dict:
    production = _candidate("production", mae=1.0)
    return {
        "schema_version": 2,
        "generated_at": "2026-09-05T20:00:00+00:00",
        "recommendation": "candidate_worth_review",
        "selected": challenger,
        "comparison": {
            "significance": {
                "candidate_vs_production": {"mae_pct": {"conclusion": production_conclusion}},
                "candidate_vs_persistence": {"mae_pct": {"conclusion": persistence_conclusion}},
            }
        },
        "candidates": [production, challenger],
    }


def _health(*, drift: str = "none", open_circuits: list[str] | None = None) -> dict:
    return {
        "available": True,
        "drift_severity": drift,
        "open_circuits": list(open_circuits or []),
        "overall_health": "open" if open_circuits else "healthy",
    }


class PromotionPolicyTests(unittest.TestCase):
    def test_good_candidate_is_eligible_for_review(self) -> None:
        challenger = _candidate(
            "good",
            mae=0.95,
            fold_maes=(0.95, 0.96, 0.94),
        )
        decision = evaluate_promotion(_report(challenger), health=_health())
        self.assertEqual(decision["decision"], "review")
        self.assertEqual(decision["reasons"], ["all_promotion_guardrails_passed"])
        self.assertTrue(all(decision["checks"]["hard_veto"].values()))
        self.assertTrue(all(decision["checks"]["review_requirements"].values()))

    def test_material_protected_horizon_regression_is_hard_veto(self) -> None:
        challenger = _candidate(
            "bad-2h",
            mae=0.95,
            horizon_maes={"2h": 1.06, "4h": 0.90, "8h": 0.90, "16h": 0.90},
            fold_maes=(0.95, 0.95, 0.95),
        )
        decision = evaluate_promotion(_report(challenger), health=_health())
        self.assertEqual(decision["decision"], "reject")
        self.assertFalse(decision["checks"]["hard_veto"]["no_material_horizon_regression"])

    def test_material_regime_regression_is_hard_veto_when_sample_is_large_enough(self) -> None:
        challenger = _candidate(
            "bad-regime",
            mae=0.95,
            regime_maes={"2h": 1.12, "4h": 0.90, "8h": 0.90, "16h": 0.90},
            fold_maes=(0.95, 0.95, 0.95),
        )
        decision = evaluate_promotion(_report(challenger), health=_health())
        self.assertEqual(decision["decision"], "reject")
        self.assertFalse(decision["checks"]["hard_veto"]["no_material_regime_regression"])

    def test_persistence_regression_can_veto_candidate(self) -> None:
        challenger = _candidate(
            "worse-than-persistence",
            mae=0.95,
            persistence_mae=0.90,
            fold_maes=(0.95, 0.95, 0.95),
        )
        decision = evaluate_promotion(_report(challenger), health=_health())
        self.assertEqual(decision["decision"], "reject")
        self.assertFalse(decision["checks"]["hard_veto"]["no_material_persistence_regression"])

    def test_severe_drift_or_open_circuit_rejects_promotion(self) -> None:
        challenger = _candidate("good", mae=0.95, fold_maes=(0.95, 0.95, 0.95))
        severe = evaluate_promotion(_report(challenger), health=_health(drift="severe"))
        self.assertEqual(severe["decision"], "reject")
        self.assertFalse(severe["checks"]["hard_veto"]["no_severe_drift"])

        open_pipeline = evaluate_promotion(
            _report(challenger), health=_health(open_circuits=["market_data"])
        )
        self.assertEqual(open_pipeline["decision"], "reject")
        self.assertFalse(open_pipeline["checks"]["hard_veto"]["no_open_pipeline_circuits"])

    def test_warning_or_unknown_health_keeps_current_without_hard_rejection(self) -> None:
        challenger = _candidate("good", mae=0.95, fold_maes=(0.95, 0.95, 0.95))
        warning = evaluate_promotion(_report(challenger), health=_health(drift="warning"))
        self.assertEqual(warning["decision"], "keep")
        self.assertIn("requirement_not_met:drift_state_stable", warning["reasons"])

        unknown = evaluate_promotion(
            _report(challenger),
            health={
                "available": False,
                "drift_severity": "unknown",
                "open_circuits": [],
                "overall_health": "unknown",
            },
        )
        self.assertEqual(unknown["decision"], "keep")
        self.assertIn("requirement_not_met:pipeline_health_known", unknown["reasons"])

    def test_inconclusive_evidence_or_low_samples_keeps_current(self) -> None:
        challenger = _candidate("good", mae=0.95, fold_maes=(0.95, 0.95, 0.95))
        inconclusive = evaluate_promotion(
            _report(challenger, production_conclusion="inconclusive"), health=_health()
        )
        self.assertEqual(inconclusive["decision"], "keep")
        self.assertFalse(
            inconclusive["checks"]["review_requirements"]["statistically_supported_vs_production"]
        )

        small = _candidate("small", mae=0.95, samples=20, fold_maes=(0.95, 0.95, 0.95))
        low_samples = evaluate_promotion(_report(small), health=_health())
        self.assertEqual(low_samples["decision"], "keep")
        self.assertFalse(low_samples["checks"]["review_requirements"]["enough_samples"])

    def test_policy_identity_is_stable_and_changes_with_policy(self) -> None:
        first = policy_identity(PromotionPolicy())
        self.assertEqual(first, policy_identity(PromotionPolicy()))
        self.assertNotEqual(
            first,
            policy_identity(PromotionPolicy(minimum_relative_mae_improvement=0.04)),
        )

    def test_decision_report_embeds_input_hash_and_health_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            optimizer = root / "optimizer.json"
            health = root / "health.json"
            challenger = _candidate("good", mae=0.95, fold_maes=(0.95, 0.95, 0.95))
            optimizer.write_text(json.dumps(_report(challenger)), encoding="utf-8")
            health.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "updated_at": "2026-09-05T20:00:00+00:00",
                        "current_signals": {"drift_severity": "none"},
                        "stages": {
                            "market_data": {"circuit_state": "closed", "health": "healthy"},
                            "forecast": {"circuit_state": "closed", "health": "healthy"},
                            "history": {"circuit_state": "closed", "health": "healthy"},
                            "x_post": {"circuit_state": "closed", "health": "healthy"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            decision = build_decision_report(optimizer, health_path=health)
            self.assertEqual(decision["decision"], "review")
            self.assertEqual(len(decision["optimizer_report_sha256"]), 64)
            self.assertTrue(decision["production_health"]["available"])
            self.assertEqual(decision["production_health"]["drift_severity"], "none")


if __name__ == "__main__":
    unittest.main()
