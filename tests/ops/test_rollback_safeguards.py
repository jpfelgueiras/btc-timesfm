#!/usr/bin/env python3
"""Tests for review-only post-promotion rollback safeguards."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from btc_timesfm.ops.rollback_safeguards import (
    RollbackPolicy,
    build_live_report,
    evaluate_rollback,
    policy_identity,
    retain_previous_champion,
)


def _record() -> dict:
    return {
        "previous_champion": {"configuration_id": "cfg-old", "parameters": {"max_blend": 0.5}},
        "promoted_configuration": {"configuration_id": "cfg-new", "parameters": {"max_blend": 0.8}},
    }


def _live(*, samples: int = 40, promoted_mae: float = 1.0, horizon_2h: float = 1.0) -> dict:
    return {
        "configuration_id": "cfg-new",
        "samples": samples,
        "promoted": {
            "mae_pct": promoted_mae,
            "direction_accuracy": 0.60,
            "calibration_error": 0.10,
        },
        "previous_champion": {
            "mae_pct": 1.0,
            "direction_accuracy": 0.61,
            "calibration_error": 0.08,
        },
        "by_horizon": {
            horizon: {
                "promoted_mae_pct": horizon_2h if horizon == "2h" else 1.0,
                "previous_champion_mae_pct": 1.0,
            }
            for horizon in ("2h", "4h", "8h", "16h")
        },
    }


class RollbackSafeguardTests(unittest.TestCase):
    def test_retains_previous_champion_reproducibly(self) -> None:
        decision = {"decision": "review", "policy_id": "promotion-policy-1"}
        report = {
            "champion": {"manifest": {"configuration_id": "cfg-old"}},
            "challenger": {"manifest": {"configuration_id": "cfg-new"}},
        }
        record = retain_previous_champion(decision, report)
        self.assertEqual(record["previous_champion"]["configuration_id"], "cfg-old")
        self.assertEqual(record["promoted_configuration"]["configuration_id"], "cfg-new")
        self.assertEqual(len(record["champion_report_sha256"]), 64)

    def test_transient_noise_continues_monitoring(self) -> None:
        recommendation = evaluate_rollback(_record(), _live(promoted_mae=1.08))
        self.assertEqual(recommendation["decision"], "continue_monitoring")
        self.assertEqual(recommendation["consecutive_breach_count"], 1)
        self.assertFalse(recommendation["production_mutation_performed"])

    def test_sustained_degradation_recommends_reviewed_rollback(self) -> None:
        first = evaluate_rollback(_record(), _live(promoted_mae=1.08))
        second = evaluate_rollback(_record(), _live(promoted_mae=1.08), prior_state=first)
        self.assertEqual(second["decision"], "recommend_rollback")
        self.assertEqual(second["rollback_reason"], "sustained_live_degradation")
        self.assertEqual(second["live_evidence"]["samples"], 40)
        self.assertTrue(second["review_required"])

    def test_protected_horizon_veto_recommends_rollback(self) -> None:
        recommendation = evaluate_rollback(_record(), _live(horizon_2h=1.06))
        self.assertEqual(recommendation["decision"], "recommend_rollback")
        self.assertEqual(recommendation["rollback_reason"], "protected_horizon_veto")
        self.assertEqual(recommendation["live_evidence"]["protected_horizon_failures"], ["2h"])

    def test_insufficient_samples_cannot_trigger_rollback(self) -> None:
        recommendation = evaluate_rollback(_record(), _live(samples=10, horizon_2h=1.20))
        self.assertEqual(recommendation["decision"], "continue_monitoring")
        self.assertFalse(recommendation["checks"]["enough_live_samples"])

    def test_builds_live_evidence_from_durable_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.sqlite"
            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE forecast_origins (origin_at TEXT PRIMARY KEY, configuration_id TEXT);
                    CREATE TABLE forecast_predictions (
                        origin_at TEXT, model_name TEXT, horizon_hours INTEGER, target_at TEXT,
                        actual_target_price_usd REAL, absolute_error_pct REAL,
                        direction_correct INTEGER, within_q10_q90 INTEGER
                    );
                    """
                )
                connection.executemany(
                    "INSERT INTO forecast_origins VALUES (?, ?)",
                    [("old", "cfg-old"), ("new", "cfg-new")],
                )
                connection.executemany(
                    "INSERT INTO forecast_predictions VALUES (?, 'ensemble', 2, ?, 1, ?, ?, ?)",
                    [
                        ("old", "2026-01-01T02:00:00Z", 1.0, 1, 1),
                        ("new", "2026-01-02T02:00:00Z", 1.1, 0, 0),
                    ],
                )
            report = build_live_report(path, "cfg-new")
        self.assertEqual(report["samples"], 1)
        self.assertEqual(report["by_horizon"]["2h"]["promoted_mae_pct"], 1.1)

    def test_policy_identity_changes_with_machine_readable_threshold(self) -> None:
        self.assertNotEqual(
            policy_identity(RollbackPolicy()),
            policy_identity(RollbackPolicy(maximum_relative_mae_degradation=0.06)),
        )


if __name__ == "__main__":
    unittest.main()
