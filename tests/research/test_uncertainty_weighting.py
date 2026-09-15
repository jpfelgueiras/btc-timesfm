#!/usr/bin/env python3
"""Tests for uncertainty-aware ensemble weighting evaluation (#131)."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from btc_timesfm.forecasting.forecast_engine import ADAPTIVE_MAX_WEIGHT, ADAPTIVE_MIN_WEIGHT
from btc_timesfm.research.experiment_registry import ExperimentRegistry
from btc_timesfm.research.uncertainty_weighting import (
    MAX_TOTAL_DOMINANCE_RATIO,
    MIN_CALIBRATION_SAMPLES,
    MIN_DOMINANCE_EVIDENCE_SAMPLES,
    calibrated_uncertainty,
    dominance_guard,
    evaluate_uncertainty_weighting,
    register_from_report,
    render_markdown,
    uncertainty_adjustments,
    uncertainty_aware_model_weights,
    weighting_formula,
)

MODELS = ["timesfm_168h", "timesfm_336h", "ar1", "persistence"]
HORIZONS = (2, 4, 8, 16)
WIDE_INTERVAL = 0.005
NARROW_INTERVAL = 0.0004


def _build_samples(
    count: int,
    *,
    intervals: bool = True,
    overconfident_model: str | None = None,
) -> tuple[list[dict], dict[int, float]]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    samples: list[dict] = []
    actuals: dict[int, float] = {}
    for i in range(count):
        origin = start + timedelta(hours=i * 2)
        current = 100.0 + i * 0.1
        model_predictions: dict[str, dict[str, dict]] = {}
        for name, offset in (
            ("timesfm_168h", 0.3),
            ("timesfm_336h", 0.31),
            ("ar1", -0.2 if i % 2 else 0.2),
            ("persistence", 0.0),
        ):
            point = current + offset
            if intervals:
                half = NARROW_INTERVAL if name == overconfident_model else WIDE_INTERVAL
                q10: float | None = point * (1.0 - half)
                q90: float | None = point * (1.0 + half)
            else:
                q10 = q90 = None
            model_predictions[name] = {
                f"{hour}h": {"price_usd": point, "q10_usd": q10, "q90_usd": q90}
                for hour in HORIZONS
            }
        sample_actuals: dict[str, float] = {}
        for hour in HORIZONS:
            target = int((origin + timedelta(hours=hour)).timestamp())
            value = current + 0.15
            actuals[target] = value
            sample_actuals[f"{hour}h"] = value
        samples.append(
            {
                "origin_timestamp": int(origin.timestamp()),
                "current_price": current,
                "actuals": sample_actuals,
                "forecast": {
                    "latest_close_at": origin.isoformat(),
                    "latest_close_usd": current,
                    "regime": "range",
                    "model_predictions": model_predictions,
                    "predictions": {},
                },
            }
        )
    return samples, actuals


def _history(samples: list[dict]) -> list[dict]:
    return [
        {
            "latest_close_at": sample["forecast"]["latest_close_at"],
            "latest_close_usd": sample["forecast"]["latest_close_usd"],
            "regime": sample["forecast"]["regime"],
            "model_predictions": sample["forecast"]["model_predictions"],
            "predictions": sample["forecast"].get("predictions", {}),
            "_outcomes": sample["forecast"].get("_outcomes", {}),
        }
        for sample in samples
    ]


class CalibratedUncertaintyTests(unittest.TestCase):
    def test_calibrated_interval_and_direction_uncertainty_use_matured_outcomes(self) -> None:
        samples, actuals = _build_samples(40, overconfident_model="timesfm_168h")
        info = calibrated_uncertainty(_history(samples), actuals, MODELS, 2)
        calibrated = info["timesfm_168h"]
        self.assertGreaterEqual(calibrated["interval_samples"], MIN_CALIBRATION_SAMPLES)
        self.assertGreaterEqual(calibrated["direction_samples"], MIN_CALIBRATION_SAMPLES)
        self.assertIsNotNone(calibrated["coverage"])
        self.assertLess(float(calibrated["coverage"]), 0.65)
        self.assertIsNotNone(calibrated["coverage_error"])
        self.assertIsNotNone(calibrated["interval_width_pct"])
        self.assertTrue(calibrated["overconfident"])
        self.assertEqual(calibrated["calibration_state"], "calibrated")

    def test_wide_intervals_are_not_flagged_overconfident(self) -> None:
        samples, actuals = _build_samples(40)
        info = calibrated_uncertainty(_history(samples), actuals, MODELS, 2)
        self.assertFalse(info["ar1"]["overconfident"])
        self.assertGreater(float(info["ar1"]["coverage"]), 0.8)

    def test_sparse_calibration_state_falls_back_with_no_penalty(self) -> None:
        uncertainty = {
            "a": {
                "samples": 2,
                "calibration_state": "sparse_fallback",
                "coverage_error": 0.35,
                "direction_brier_error": 0.4,
            },
            "b": {
                "samples": 0,
                "calibration_state": "sparse_fallback",
                "coverage_error": None,
                "direction_brier_error": None,
            },
        }
        penalties, diagnostics = uncertainty_adjustments(uncertainty)
        self.assertEqual(penalties["a"], 1.0)
        self.assertEqual(penalties["b"], 1.0)
        self.assertEqual(diagnostics["a"]["calibration_state"], "sparse_fallback")
        self.assertEqual(diagnostics["a"]["blend"], 0.0)


class WeightFormulaTests(unittest.TestCase):
    def test_weight_formula_is_versioned_and_json_serializable(self) -> None:
        formula = weighting_formula()
        self.assertEqual(formula["version"], 1)
        self.assertEqual(formula["promotion_mode"], "report_only")
        self.assertEqual(formula["evidence_policy"], "paired_bootstrap_comparison")
        self.assertIn("correlation_overlay", formula)
        text = json.dumps(formula, sort_keys=True)
        self.assertIn("weight_cap", dict(json.loads(text)))

    def test_weights_are_reproducible(self) -> None:
        samples, actuals = _build_samples(40)
        history = _history(samples)
        first = uncertainty_aware_model_weights(MODELS, "range", 2, history, actuals)
        second = uncertainty_aware_model_weights(MODELS, "range", 2, history, actuals)
        self.assertEqual(first, second)


class UncertaintyWeightsTests(unittest.TestCase):
    def test_weights_are_bounded_and_normalized(self) -> None:
        samples, actuals = _build_samples(40)
        history = _history(samples)
        weights, diagnostics = uncertainty_aware_model_weights(MODELS, "range", 2, history, actuals)
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=9)
        for weight in weights.values():
            self.assertGreaterEqual(weight, ADAPTIVE_MIN_WEIGHT - 1e-9)
            self.assertLessEqual(weight, ADAPTIVE_MAX_WEIGHT + 1e-9)
        self.assertIn(diagnostics["uncertainty_mode"], {"active", "base_policy"})

    def test_candidate_combines_correlation_overlay_and_calibration(self) -> None:
        samples, actuals = _build_samples(40)
        history = _history(samples)
        _, diagnostics = uncertainty_aware_model_weights(MODELS, "range", 2, history, actuals)
        if diagnostics["uncertainty_mode"] == "active":
            self.assertIn("base_policy_mode", diagnostics)
            self.assertIn("residual_correlations", diagnostics)
            self.assertIn("residual_pair_samples", diagnostics)
            self.assertIn("calibrated_uncertainty", diagnostics)
            self.assertIn("uncertainty_penalties", diagnostics)
            self.assertIn("dominance_guard", diagnostics)
            self.assertIn("weighting_formula", diagnostics)

    def test_insufficient_history_leaves_base_policy_unchanged(self) -> None:
        samples, actuals = _build_samples(3)
        weights, diagnostics = uncertainty_aware_model_weights(
            MODELS, "range", 2, _history(samples), actuals
        )
        self.assertEqual(diagnostics["uncertainty_mode"], "base_policy")
        self.assertTrue(diagnostics["uncertainty_reason"].startswith("base_policy"))
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=9)
        for weight in weights.values():
            self.assertGreaterEqual(weight, ADAPTIVE_MIN_WEIGHT - 1e-9)
            self.assertLessEqual(weight, ADAPTIVE_MAX_WEIGHT + 1e-9)

    def test_interval_free_history_runs_sparse_calibration_fallback(self) -> None:
        samples, actuals = _build_samples(40, intervals=False)
        history = _history(samples)
        info = calibrated_uncertainty(history, actuals, MODELS, 2)
        self.assertTrue(
            all(entry["calibration_state"] == "sparse_fallback" for entry in info.values())
        )
        _, diagnostics = uncertainty_aware_model_weights(MODELS, "range", 2, history, actuals)
        if diagnostics["uncertainty_mode"] == "active":
            self.assertTrue(
                all(
                    penalties["penalty"] == 1.0
                    for penalties in diagnostics["uncertainty_penalties"].values()
                )
            )

    def test_future_actuals_do_not_leak_into_evaluation(self) -> None:
        samples, actuals = _build_samples(40)
        report = evaluate_uncertainty_weighting(samples, actuals)
        leaked = dict(actuals)
        last_origin = samples[-1]["origin_timestamp"]
        leaked[last_origin + 2 * 3600] = 12345.0
        leaked[last_origin + 500 * 3600] = 99999.0
        comparison = evaluate_uncertainty_weighting(samples, leaked)
        self.assertEqual(
            report["reproducibility"]["audit_sha256"],
            comparison["reproducibility"]["audit_sha256"],
        )


class DominanceGuardTests(unittest.TestCase):
    def _uncertainty(self, leader: dict) -> dict:
        return {
            "a": leader,
            "b": {"samples": 8, "overconfident": False},
            "c": {"samples": 8, "overconfident": False},
        }

    def test_unverified_leader_is_clipped_to_bounded_lead(self) -> None:
        raw = {"a": 10.0, "b": 1.0, "c": 1.1}
        guarded, info = dominance_guard(
            raw, self._uncertainty({"samples": 4, "overconfident": False})
        )
        self.assertTrue(info["applied"])
        self.assertEqual(info["clipped_models"], ["a"])
        self.assertAlmostEqual(guarded["a"], 1.1 * MAX_TOTAL_DOMINANCE_RATIO)
        self.assertEqual(guarded["b"], 1.0)

    def test_overconfident_leader_cannot_dominate_without_calibrated_evidence(self) -> None:
        raw = {"a": 10.0, "b": 1.0, "c": 1.1}
        guarded, info = dominance_guard(
            raw,
            self._uncertainty(
                {"samples": MIN_DOMINANCE_EVIDENCE_SAMPLES + 10, "overconfident": True}
            ),
        )
        self.assertTrue(info["applied"])
        self.assertTrue(info["overconfident_leader"])
        self.assertAlmostEqual(guarded["a"], 1.1 * MAX_TOTAL_DOMINANCE_RATIO)

    def test_evidenced_calibrated_model_keeps_lead(self) -> None:
        raw = {"a": 10.0, "b": 1.0, "c": 1.1}
        guarded, info = dominance_guard(
            raw,
            self._uncertainty({"samples": MIN_DOMINANCE_EVIDENCE_SAMPLES, "overconfident": False}),
        )
        self.assertFalse(info["applied"])
        self.assertAlmostEqual(guarded["a"], 10.0)

    def test_single_model_never_triggers_guard(self) -> None:
        guarded, info = dominance_guard({"only": 1.0}, {})
        self.assertFalse(info["applied"])
        self.assertEqual(guarded["only"], 1.0)


class EvaluationTests(unittest.TestCase):
    def test_report_compares_candidate_against_mandatory_baselines(self) -> None:
        samples, actuals = _build_samples(40)
        report = evaluate_uncertainty_weighting(samples, actuals)
        self.assertEqual(report["samples"], 40)
        self.assertEqual(set(report["horizons"]), {"2h", "4h", "8h", "16h"})
        for horizon in report["horizons"]:
            policies = report["by_horizon"][horizon]["policies"]
            self.assertEqual(
                set(policies), {"adaptive", "correlation_aware", "uncertainty_aware", "persistence"}
            )
            self.assertEqual(policies["uncertainty_aware"]["samples"], 40)
            self.assertEqual(policies["adaptive"]["samples"], 40)
            self.assertEqual(policies["correlation_aware"]["samples"], 40)
            self.assertEqual(policies["persistence"]["samples"], 40)
            self.assertIn("uncertainty_minus_correlation_mae_pct", report["by_horizon"][horizon])
        self.assertIn("range", report["by_regime"])
        self.assertEqual(report["promotion"]["mode"], "report_only")
        self.assertEqual(
            report["mandatory_baselines"],
            ["adaptive_weighting", "correlation_weighting", "persistence"],
        )
        self.assertIn("uncertainty_vs_correlation_aware", report["significance"]["overall"])
        self.assertIn("uncertainty_vs_persistence", report["significance"]["overall"])
        for horizon in report["horizons"]:
            significance = report["significance"][horizon]
            self.assertIn("uncertainty_vs_adaptive", significance)
            self.assertIn("uncertainty_vs_correlation_aware", significance)

    def test_report_audit_hash_is_reproducible(self) -> None:
        samples, actuals = _build_samples(40)
        first = evaluate_uncertainty_weighting(samples, actuals)
        second = evaluate_uncertainty_weighting(samples, actuals)
        self.assertEqual(
            first["reproducibility"]["audit_sha256"],
            second["reproducibility"]["audit_sha256"],
        )
        self.assertEqual(first["weighting_formula"], second["weighting_formula"])

    def test_input_order_does_not_change_report(self) -> None:
        samples, actuals = _build_samples(40)
        report = evaluate_uncertainty_weighting(list(reversed(samples)), actuals)
        self.assertEqual(report["samples"], 40)
        self.assertIn("range", report["by_regime"])

    def test_registers_report_as_candidate_experiment(self) -> None:
        samples, actuals = _build_samples(40)
        report = evaluate_uncertainty_weighting(samples, actuals)
        with tempfile.TemporaryDirectory() as tmp:
            registry = ExperimentRegistry(Path(tmp) / "experiments.db")
            manifest, record, created = register_from_report(report, registry)
            self.assertTrue(created)
            self.assertEqual(record["decision"], "candidate")
            self.assertEqual(record["run_type"], "uncertainty_weighting")
            self.assertIn("run_id", manifest)

    def test_markdown_report_renders(self) -> None:
        samples, actuals = _build_samples(40)
        report = evaluate_uncertainty_weighting(samples, actuals)
        text = render_markdown(report)
        self.assertIn("# Uncertainty-aware ensemble weighting", text)
        self.assertIn("Promotion mode", text)
        self.assertIn("## By horizon", text)
        self.assertIn("## By regime", text)


if __name__ == "__main__":
    unittest.main()
