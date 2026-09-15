#!/usr/bin/env python3
"""Tests for no-lookahead recency-aware adaptation evaluation."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from btc_timesfm.ops.drift_detection import DriftConfig
from btc_timesfm.research.recency_adaptation import (
    PRODUCTION_POLICY_NAME,
    RecencyConfig,
    evaluate_recency_adaptation,
    recency_model_weights,
    register_evaluation,
)

MODEL_NAMES = ["timesfm_168h", "timesfm_336h", "ar1", "persistence"]
HORIZONS = (2, 4, 8, 16)
PRODUCTION_ANCHOR = {
    "timesfm_168h": 0.35,
    "timesfm_336h": 0.25,
    "ar1": 0.20,
    "persistence": 0.20,
}


def _baseline_features() -> dict:
    return {
        "volatility_24h_pct": 0.30,
        "range_24h_avg_pct": 0.50,
        "volume_zscore_7d": 0.20,
        "rsi_14": 52.0,
        "momentum_24h_pct": 0.10,
    }


def _jump_features(i: int) -> dict:
    features = _baseline_features()
    if i >= 38:
        features["volatility_24h_pct"] = 3.0
        features["momentum_24h_pct"] = 5.0
        features["rsi_14"] = 82.0
    return features


def _make_samples(
    count: int,
    *,
    step_hours: int = 2,
    mode: str = "range",
    feature_fn: Callable[[int], dict] | None = None,
) -> tuple[list[dict], dict[int, float]]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    samples: list[dict] = []
    actuals: dict[int, float] = {}
    for i in range(count):
        origin = start + timedelta(hours=step_hours * i)
        current = 100.0 + i * 0.1
        actual_value = current + 0.15
        predictions: dict[str, dict[str, dict[str, float]]] = {}
        for name, offset in (
            ("timesfm_168h", -0.05 if i % 2 else 0.05),
            ("timesfm_336h", 2.85),
            ("ar1", 0.0),
            ("persistence", -0.15),
        ):
            predictions[name] = {
                f"{hour}h": {"price_usd": current + 0.15 + offset} for hour in HORIZONS
            }
        sample_actuals = {}
        for hour in HORIZONS:
            target = int((origin + timedelta(hours=hour)).timestamp())
            actuals[target] = actual_value
            sample_actuals[f"{hour}h"] = actual_value
        regime_name = mode(i) if callable(mode) else str(mode)
        samples.append(
            {
                "origin_timestamp": int(origin.timestamp()),
                "origin_at": origin.isoformat(),
                "current_price": current,
                "actuals": sample_actuals,
                "forecast": {
                    "latest_close_at": origin.isoformat(),
                    "latest_close_usd": current,
                    "regime": regime_name,
                    "market_features": (feature_fn(i) if feature_fn else _baseline_features()),
                    "model_predictions": predictions,
                    "predictions": {},
                },
            }
        )
    return samples, actuals


def _core(report: dict[str, Any]) -> dict[str, Any]:
    return {
        key: report[key]
        for key in (
            "samples_evaluated",
            "by_horizon",
            "by_segment",
            "responsiveness_stability",
            "guardrails",
            "significance",
            "leakage_safety",
            "drift_segmentation",
        )
    }


class RecencyAdaptationReportTests(unittest.TestCase):
    def test_report_contains_policies_segments_guardrails_and_promotion(self) -> None:
        samples, actuals = _make_samples(48)
        report = evaluate_recency_adaptation(
            samples,
            actuals,
            folds=2,
            min_train_samples=10,
            drift_config=DriftConfig(),
        )
        self.assertEqual(report["schema_version"], 1)
        self.assertGreater(report["samples_evaluated"], 0)
        self.assertEqual(report["horizons"], ["2h", "4h", "8h", "16h"])
        self.assertTrue(report["leakage_safety"]["purged_walk_forward"])
        self.assertTrue(report["leakage_safety"]["origin_time_only_actuals"])
        self.assertTrue(report["leakage_safety"]["assert_fold_leakage"])

        for horizon in report["by_horizon"]:
            block = report["by_horizon"][horizon]
            self.assertIn("production_adaptive", block)
            self.assertIn("persistence", block)
            self.assertTrue(block["policies"])
        self.assertIn("drift", report["by_segment"])
        self.assertIn("normal", report["by_segment"])

        stability = report["responsiveness_stability"]["by_policy"]
        for name in (PRODUCTION_POLICY_NAME, "rolling_24", "recency_exp_6"):
            self.assertIn(name, stability)
            self.assertIn("responsiveness_score", stability[name])
            self.assertIn("stability_score", stability[name])

        policies_in_report = set(report["policy_configs"]) - {PRODUCTION_POLICY_NAME}
        for name in policies_in_report:
            guardrail = report["guardrails"]["by_policy"][name]
            self.assertIsInstance(guardrail["insufficient_history_origins"], int)
            self.assertIsInstance(guardrail["weight_shift_capped_origins"], int)

        for name in policies_in_report:
            self.assertIn(name, report["significance"]["vs_production"])

        promotion = report["promotion"]
        self.assertTrue(promotion["available"])
        self.assertTrue(promotion["report_only"])
        self.assertIn("decision", promotion)
        self.assertIn("champion_challenger", promotion)
        self.assertEqual(promotion["candidate_metrics"], report["metrics_for_registry"])
        self.assertIn("experiment_manifest", report)
        self.assertEqual(report["experiment_manifest"]["run_type"], "recency_adaptation_evaluation")

    def test_order_of_input_samples_does_not_change_report(self) -> None:
        samples, actuals = _make_samples(26)
        baseline = evaluate_recency_adaptation(
            samples,
            actuals,
            folds=2,
            min_train_samples=10,
            bootstrap_iterations=300,
        )
        shuffled = list(reversed(samples))
        runner = evaluate_recency_adaptation(
            shuffled,
            actuals,
            folds=2,
            min_train_samples=10,
            bootstrap_iterations=300,
        )
        self.assertEqual(_core(runner), _core(baseline))
        self.assertEqual(runner["samples_evaluated"], baseline["samples_evaluated"])


class RecencyLeakageTests(unittest.TestCase):
    def test_future_actuals_cannot_affect_weights_at_an_origin(self) -> None:
        samples, actuals = _make_samples(8)
        config = RecencyConfig(
            name="rolling_window",
            technique="rolling_window",
            history_limit=10,
            min_effective_samples=2,
            full_effective_samples=8,
            max_blend=0.5,
            max_weight_shift=0.9,
        )
        current_timestamp = int(samples[3]["origin_timestamp"])
        history = [sample["forecast"] for sample in samples]

        weights, diagnostics = recency_model_weights(
            MODEL_NAMES,
            "range",
            2,
            history,
            actuals,
            current_timestamp=current_timestamp,
            config=config,
            production_weights=PRODUCTION_ANCHOR,
        )
        self.assertNotEqual(diagnostics["source"], "insufficient_history")
        self.assertEqual(diagnostics["models"]["ar1"]["weighted_metrics"]["samples"], 3)

        future_actuals = dict(actuals)
        future_target = int(samples[0]["origin_timestamp"]) + 16 * 3600
        self.assertGreater(future_target, current_timestamp)
        future_actuals[future_target] = 999999.0
        weights_future, diagnostics_future = recency_model_weights(
            MODEL_NAMES,
            "range",
            2,
            history,
            future_actuals,
            current_timestamp=current_timestamp,
            config=config,
            production_weights=PRODUCTION_ANCHOR,
        )
        self.assertEqual(weights_future, weights)
        self.assertEqual(diagnostics_future, diagnostics)

    def test_report_is_invariant_to_unmatured_target_actuals(self) -> None:
        samples, actuals = _make_samples(32)
        future_actuals = dict(actuals)
        future_target = int(samples[-1]["origin_timestamp"]) + 1000 * 3600
        future_actuals[future_target] = 777777.0
        baseline = evaluate_recency_adaptation(
            samples,
            actuals,
            folds=2,
            min_train_samples=10,
            bootstrap_iterations=200,
        )
        runner = evaluate_recency_adaptation(
            samples,
            future_actuals,
            folds=2,
            min_train_samples=10,
            bootstrap_iterations=200,
        )
        self.assertEqual(_core(runner), _core(baseline))


class RecencyGuardrailTests(unittest.TestCase):
    def test_small_sample_guardrail_anchors_exactly_to_production(self) -> None:
        samples, actuals = _make_samples(4)
        config = RecencyConfig(
            name="uniform",
            technique="uniform",
            min_effective_samples=6,
            full_effective_samples=24,
            max_blend=0.5,
        )
        current_timestamp = int(samples[3]["origin_timestamp"])
        weights, diagnostics = recency_model_weights(
            MODEL_NAMES,
            "range",
            2,
            [sample["forecast"] for sample in samples],
            actuals,
            current_timestamp=current_timestamp,
            config=config,
            production_weights=PRODUCTION_ANCHOR,
        )
        self.assertEqual(weights, PRODUCTION_ANCHOR)
        self.assertEqual(diagnostics["mode"], "static_prior")
        self.assertEqual(diagnostics["source"], "insufficient_history")
        self.assertIn("guardrail", diagnostics)

    def test_extreme_weight_shift_is_capped_at_max_weight_shift(self) -> None:
        samples, actuals = _make_samples(24)
        config = RecencyConfig(
            name="rolling_window",
            technique="rolling_window",
            history_limit=12,
            min_effective_samples=4,
            full_effective_samples=24,
            max_blend=1.0,
            max_weight_shift=0.05,
        )
        current_timestamp = int(samples[-1]["origin_timestamp"])
        weights, diagnostics = recency_model_weights(
            MODEL_NAMES,
            "range",
            2,
            [sample["forecast"] for sample in samples],
            actuals,
            current_timestamp=current_timestamp,
            config=config,
            production_weights=PRODUCTION_ANCHOR,
        )
        self.assertTrue(diagnostics["weight_shift_capped"])
        for name in MODEL_NAMES:
            self.assertLessEqual(
                abs(weights[name] - PRODUCTION_ANCHOR[name]),
                config.max_weight_shift + 1e-9,
            )
        self.assertLessEqual(diagnostics["blend_factor"], config.max_blend)
        self.assertEqual(sum(weights.values()), 1.0)

    def test_guardrail_counts_are_reported_without_touching_production_defaults(self) -> None:
        samples, actuals = _make_samples(20)
        report = evaluate_recency_adaptation(
            samples,
            actuals,
            folds=2,
            min_train_samples=10,
            bootstrap_iterations=200,
        )
        configs = report["policy_configs"]
        for name in configs:
            if name == PRODUCTION_POLICY_NAME:
                continue
            guardrail = report["guardrails"]["by_policy"][name]
            self.assertIsInstance(guardrail["anchored_to_production_origins"], int)
        self.assertTrue(report["promotion"]["report_only"])
        self.assertFalse(report["promotion"]["review_contract"]["production_changes_automatic"])


class RecencySegmentTests(unittest.TestCase):
    def test_drift_and_normal_periods_are_evaluated_separately(self) -> None:
        samples, actuals = _make_samples(48, feature_fn=_jump_features)
        drift_config = DriftConfig(
            error_recent=4,
            error_baseline=4,
            feature_recent=4,
            feature_baseline=4,
            warning_shift_z=2.0,
            severe_shift_z=3.5,
            warning_ks=0.2,
            severe_ks=0.6,
        )
        report = evaluate_recency_adaptation(
            samples,
            actuals,
            folds=2,
            min_train_samples=10,
            drift_config=drift_config,
            bootstrap_iterations=300,
        )
        origins = report["drift_segmentation"]["origins"]
        self.assertGreater(origins["drift"], 0)
        self.assertGreater(origins["normal"], 0)
        self.assertEqual(origins["total"], report["samples_evaluated"])
        self.assertIn("drift", report["by_segment"])
        drift_horizon = report["by_segment"]["drift"]["2h"]
        self.assertIn("production_adaptive", drift_horizon["policies"])
        self.assertIn("drift", report["significance"]["by_segment"])


class RecencyRegistryTests(unittest.TestCase):
    def test_evaluation_registers_idempotently_into_experiment_registry(self) -> None:
        samples, actuals = _make_samples(20)
        report = evaluate_recency_adaptation(
            samples,
            actuals,
            folds=2,
            min_train_samples=10,
            bootstrap_iterations=200,
        )
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "registry.sqlite"
            first = register_evaluation(report, db_path=db_path)
            self.assertTrue(first["created"])
            second = register_evaluation(report, db_path=db_path)
            self.assertFalse(second["created"])
            self.assertEqual(first["run_id"], second["run_id"])
            self.assertEqual(second["record"]["decision"], "candidate")
            self.assertIn("#130", second["record"]["hypothesis"])
            self.assertEqual(second["record"]["run_type"], "recency_adaptation_evaluation")
            self.assertIsInstance(second["record"]["metrics"], dict)


if __name__ == "__main__":
    unittest.main()
