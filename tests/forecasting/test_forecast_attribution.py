#!/usr/bin/env python3
"""Unit tests for forecast explanation and attribution output (#127)."""

from __future__ import annotations

import json
import unittest
from typing import Any

from tests.support.unit_test_stubs import install_timesfm_stub

install_timesfm_stub()

from btc_timesfm.cli import btc_forecast  # noqa: E402
from btc_timesfm.forecasting.forecast_attribution import (  # noqa: E402
    FORECAST_ATTRIBUTION_VERSION,
    build_forecast_attribution,
    explain_regime,
)

REGIME_DRIVERS = ("volatility_24h_pct", "volatility_7d_pct", "momentum_24h_pct", "rsi_14")


def weights() -> dict[str, dict[str, float]]:
    return {
        horizon: {
            "timesfm_512h": 0.40,
            "timesfm_336h": 0.25,
            "timesfm_168h": 0.20,
            "persistence": 0.09,
            "drift_7d": 0.03,
            "ar1": 0.03,
        }
        for horizon in ("2h", "4h", "8h", "16h")
    }


def diagnostics() -> dict[str, Any]:
    return {
        horizon: {
            "mode": "adaptive",
            "source": "regime",
            "horizon": horizon,
            "regime": "range",
            "sample_count": 18,
            "persistence_mae_pct": 0.92,
            "models": {
                "timesfm_512h": {
                    "samples": 18,
                    "mae_pct": 0.80,
                    "direction_accuracy": 0.56,
                    "prior_weight": 0.228,
                    "raw_score": 0.40,
                    "adaptive_weight": 0.45,
                    "final_weight": 0.40,
                    "edge_vs_persistence_mae_pct": 0.12,
                },
                "timesfm_336h": {
                    "samples": 18,
                    "mae_pct": 0.72,
                    "direction_accuracy": 0.54,
                    "prior_weight": 0.199,
                    "raw_score": 0.35,
                    "adaptive_weight": 0.28,
                    "final_weight": 0.25,
                    "edge_vs_persistence_mae_pct": 0.20,
                },
                "timesfm_168h": {
                    "samples": 18,
                    "mae_pct": 0.76,
                    "direction_accuracy": 0.53,
                    "prior_weight": 0.142,
                    "raw_score": 0.30,
                    "adaptive_weight": 0.22,
                    "final_weight": 0.20,
                    "edge_vs_persistence_mae_pct": 0.16,
                },
                "persistence": {
                    "samples": 18,
                    "mae_pct": 0.92,
                    "direction_accuracy": 0.51,
                    "prior_weight": 0.240,
                    "raw_score": 0.06,
                    "adaptive_weight": 0.05,
                    "final_weight": 0.09,
                    "edge_vs_persistence_mae_pct": 0.0,
                },
                "drift_7d": {
                    "samples": 18,
                    "mae_pct": 0.91,
                    "direction_accuracy": 0.52,
                    "prior_weight": 0.060,
                    "raw_score": 0.04,
                    "adaptive_weight": 0.0,
                    "final_weight": 0.03,
                    "edge_vs_persistence_mae_pct": 0.01,
                },
                "ar1": {
                    "samples": 18,
                    "mae_pct": 0.90,
                    "direction_accuracy": 0.51,
                    "prior_weight": 0.130,
                    "raw_score": 0.03,
                    "adaptive_weight": 0.0,
                    "final_weight": 0.03,
                    "edge_vs_persistence_mae_pct": 0.02,
                },
            },
        }
        for horizon in ("2h", "4h", "8h", "16h")
    }


def predictions() -> dict[str, dict[str, float]]:
    return {
        horizon: {
            "price_usd": 100920.0,
            "change_pct": 0.32,
            "model_agreement": 0.8,
            "model_disagreement_pct": 0.12,
            "model_disagreement_usd": 120.0,
        }
        for horizon in ("2h", "4h", "8h", "16h")
    }


def features() -> dict[str, float]:
    return {
        "close_usd": 100000.0,
        "volatility_6h_pct": 0.55,
        "volatility_24h_pct": 0.9,
        "volatility_7d_pct": 1.0,
        "range_24h_avg_pct": 0.8,
        "volume_zscore_7d": 0.4,
        "rsi_14": 52.0,
        "momentum_6h_pct": 0.05,
        "momentum_24h_pct": 0.22,
        "momentum_7d_pct": 1.5,
        "hour_utc": 12,
        "weekday_utc": 3,
        "derivatives_funding_rate_pct": 0.01,
        "derivatives_liquidation_imbalance": 0.2,
        "microstructure_spread_bps": 8.0,
        "cross_eth_return_24h_pct": 1.1,
    }


def thresholds(edge_status: str = "edge") -> dict[str, Any]:
    return {
        "version": 1,
        "public_directional_claim_allowed": edge_status == "edge",
        "horizons": {
            horizon: {
                "horizon": horizon,
                "samples": 30,
                "reliable": edge_status != "insufficient_evidence",
                "realized_volatility_pct": 1.2,
                "mean_actual_change_pct": 0.05,
                "edge_status": edge_status,
                "suppress_directional_claim": edge_status
                in {
                    "no_edge",
                    "insufficient_evidence",
                },
                "suppress_reasons": (
                    ["predicted move is inside the no-edge noise band"]
                    if edge_status == "no_edge"
                    else ["only 0 matured samples, below the 20 evidence minimum"]
                    if edge_status == "insufficient_evidence"
                    else []
                ),
                "baseline_evaluation": {
                    "samples": 30,
                    "agreement_rate": 0.8,
                    "dynamic_neutral_rate": 0.1,
                },
            }
            for horizon in ("2h", "4h", "8h", "16h")
        },
    }


def probabilities() -> dict[str, Any]:
    return {
        "version": 1,
        "public_claim_allowed": True,
        "horizons": {
            horizon: {
                "horizon": horizon,
                "samples": 30,
                "reliable": True,
                "calibration_state": "calibrated",
                "p_up": 0.62,
                "p_down": 0.34,
                "p_move": 0.55,
                "brier_score": 0.24,
            }
            for horizon in ("2h", "4h", "8h", "16h")
        },
    }


def abstention(state: str = "healthy", reasons: list[str] | None = None) -> dict[str, Any]:
    return {
        "version": 1,
        "state": state,
        "withhold_forecast": state == "forecast_withheld",
        "public_directional_claim_allowed": state == "healthy",
        "reasons": reasons or [],
        "inputs": {},
    }


def build(
    *,
    abstention_result: dict[str, Any] | None = None,
    threshold_result: dict[str, Any] | None = None,
    regime: str = "range",
) -> dict[str, Any]:
    return build_forecast_attribution(
        predictions=predictions(),
        model_weights=weights(),
        weighting_diagnostics=diagnostics(),
        regime=regime,
        features=features(),
        abstention_policy=abstention_result if abstention_result is not None else abstention(),
        dynamic_thresholds=threshold_result if threshold_result is not None else thresholds(),
        direction_probability=probabilities(),
    )


class AttributionStructureTests(unittest.TestCase):
    def test_attribution_generated_for_all_horizons(self) -> None:
        section = build()
        self.assertEqual(section["version"], FORECAST_ATTRIBUTION_VERSION)
        self.assertEqual(set(section["horizons"]), {"2h", "4h", "8h", "16h"})
        json.dumps(section)

    def test_healthy_forecast_reports_edge_overall(self) -> None:
        section = build()
        self.assertEqual(section["abstention"]["state"], "healthy")
        self.assertEqual(section["overall"]["state"], "edge")
        self.assertEqual(section["overall"]["edge_horizons"], 4)
        self.assertEqual(section["overall"]["suppressed_horizons"], [])

    def test_top_contributors_are_weight_sorted(self) -> None:
        section = build()
        contributors = section["horizons"]["2h"]["top_contributors"]
        self.assertEqual(contributors[0]["model"], "timesfm_512h")
        self.assertEqual(contributors[0]["weight"], 0.4)
        self.assertTrue(contributors[0]["dominant"])
        self.assertLessEqual(len(contributors), 3)

    def test_dominant_models_are_flagged(self) -> None:
        section = build()
        dominant = section["horizons"]["2h"]["dominant_models"]
        self.assertEqual([item["model"] for item in dominant], ["timesfm_512h"])

    def test_model_agreement_and_disagreement_are_reported(self) -> None:
        agreement = build()["horizons"]["2h"]["model_agreement"]
        self.assertEqual(agreement["agreement"], 0.8)
        self.assertEqual(agreement["model_disagreement_pct"], 0.12)
        self.assertEqual(agreement["model_disagreement_usd"], 120.0)

    def test_feature_groups_partition_features(self) -> None:
        section = build()
        groups = section["feature_groups"]
        self.assertIn("engine", groups)
        self.assertIn("derivatives", groups)
        self.assertIn("microstructure", groups)
        self.assertIn("cross_asset", groups)
        self.assertTrue(groups["derivatives"]["feature_count"] >= 2)
        self.assertIn("volatility_24h_pct", groups["engine"]["features"])
        self.assertIn("derivatives_funding_rate_pct", groups["derivatives"]["features"])
        self.assertIn("cross_eth_return_24h_pct", groups["cross_asset"]["features"])

    def test_regime_explanation_reports_drivers_and_rule(self) -> None:
        section = build()
        explanation = section["regime_explanation"]
        self.assertEqual(explanation["regime"], "range")
        self.assertEqual(explanation["driving_feature_group"], "engine")
        self.assertTrue(explanation["rule"])
        self.assertEqual(
            {driver["feature"] for driver in explanation["drivers"]},
            set(REGIME_DRIVERS),
        )
        for driver in explanation["drivers"]:
            self.assertIn("value", driver)
            self.assertIn("role", driver)


class HistoricalEdgeTests(unittest.TestCase):
    def test_comparable_conditions_include_sample_size_context(self) -> None:
        section = build()
        for horizon in ("2h", "4h", "8h", "16h"):
            comparable = section["horizons"][horizon]["comparable_conditions"]
            self.assertEqual(comparable["condition"], f"range:{horizon}")
            self.assertGreaterEqual(int(comparable["samples"]), 0)
            context = comparable["sample_context"]
            self.assertIn("matured_comparable_samples", context)
            self.assertIn("dynamic_threshold_samples", context)
            self.assertEqual(context["dynamic_threshold_samples"], 30)

    def test_model_outcome_rows_carry_weights_and_edges(self) -> None:
        comparable = build()["horizons"]["2h"]["comparable_conditions"]
        models = comparable["models"]
        self.assertEqual(len(models), 6)
        by_name = {item["model"]: item for item in models}
        self.assertEqual(by_name["timesfm_512h"]["weight"], 0.4)
        self.assertEqual(by_name["timesfm_512h"]["samples"], 18)
        self.assertEqual(by_name["timesfm_512h"]["edge_vs_persistence_mae_pct"], 0.12)
        self.assertEqual(by_name["persistence"]["edge_vs_persistence_mae_pct"], 0.0)

    def test_weighted_edge_is_computed_from_component_edges(self) -> None:
        comparable = build()["horizons"]["2h"]["comparable_conditions"]
        weighted = comparable["weighted_edge_vs_persistence_mae_pct"]
        expected = 0.40 * 0.12 + 0.25 * 0.20 + 0.20 * 0.16 + 0.09 * 0.0 + 0.03 * 0.01 + 0.03 * 0.02
        self.assertAlmostEqual(weighted, round(expected, 6), places=6)

    def test_direction_probability_evidence_is_included(self) -> None:
        comparable = build()["horizons"]["2h"]["comparable_conditions"]
        probability = comparable["direction_probability"]
        self.assertEqual(probability["p_up"], 0.62)
        self.assertTrue(probability["reliable"])
        self.assertEqual(probability["samples"], 30)


class EvidenceStateTests(unittest.TestCase):
    def test_withheld_forecast_explains_failure_state(self) -> None:
        section = build(
            abstention_result=abstention(
                "forecast_withheld",
                reasons=["market data health is degraded"],
            )
        )
        self.assertEqual(section["abstention"]["withhold_forecast"], True)
        self.assertEqual(section["overall"]["state"], "withheld")
        for horizon in ("2h", "4h", "8h", "16h"):
            entry = section["horizons"][horizon]
            self.assertEqual(entry["edge_state"], "withheld")
            self.assertTrue(
                any("forecast is withheld" in reason for reason in entry["edge_reasons"])
            )
            self.assertEqual(entry["uncertainty"]["abstention_state"], "forecast_withheld")

    def test_no_edge_horizons_are_suppressed_with_reasons(self) -> None:
        no_edge = thresholds(edge_status="no_edge")
        section = build(threshold_result=no_edge)
        self.assertEqual(section["overall"]["state"], "suppressed")
        self.assertEqual(section["overall"]["suppressed_horizons"], ["2h", "4h", "8h", "16h"])
        entry = section["horizons"]["2h"]
        self.assertEqual(entry["edge_state"], "no_edge")
        self.assertIn("noise band", entry["edge_reasons"][0])

    def test_insufficient_evidence_state_keeps_sample_reason(self) -> None:
        sparse = thresholds(edge_status="insufficient_evidence")
        section = build(threshold_result=sparse)
        entry = section["horizons"]["2h"]
        self.assertEqual(entry["edge_state"], "insufficient_evidence")
        self.assertTrue(any("evidence minimum" in reason for reason in entry["edge_reasons"]))

    def test_low_confidence_state_is_reported(self) -> None:
        section = build(
            abstention_result=abstention("low_confidence_no_measurable_edge"),
            threshold_result=thresholds(edge_status="no_edge"),
        )
        self.assertEqual(section["overall"]["state"], "low_confidence")
        self.assertFalse(section["abstention"]["withhold_forecast"])
        self.assertEqual(section["horizons"]["2h"]["edge_state"], "no_edge")

    def test_degraded_inputs_state_is_reported(self) -> None:
        section = build(
            abstention_result=abstention(
                "degraded_inputs",
                reasons=["8h model agreement 40% is below 50%"],
            )
        )
        self.assertEqual(section["overall"]["state"], "degraded")
        acts = {h: section["horizons"][h]["edge_state"] for h in section["horizons"]}
        self.assertEqual(acts, {h: "degraded" for h in ("2h", "4h", "8h", "16h")})


class WordingPolicyTests(unittest.TestCase):
    FORBIDDEN = ("guarantee", "guaranteed", "because of", "will move", "caused")

    def test_explanations_avoid_causal_and_performance_claims(self) -> None:
        section = build()
        narratives = [entry["explanation"] for entry in section["horizons"].values()]
        narratives.extend(
            [
                section["regime_explanation"]["rule"],
                section["meaning"],
            ]
        )
        lowered = json.dumps(narratives).lower()
        for term in self.FORBIDDEN:
            self.assertNotIn(term, lowered, term)

    def test_meaning_field_describes_evidence_not_narrative(self) -> None:
        meaning = build()["meaning"].lower()
        self.assertIn("evidence", meaning)
        self.assertIn("not causal", meaning)

    def test_edge_claims_include_sample_context_in_sentence_when_available(self) -> None:
        explanation = build()["horizons"]["2h"]["explanation"]
        self.assertIn("18 matured comparable samples", explanation)
        self.assertIn("measured weighted MAE edge versus persistence", explanation)


class ReproducibilityTests(unittest.TestCase):
    def test_attribution_is_deterministic_for_same_inputs(self) -> None:
        first = build()
        second = build()
        first.pop("generated_at")
        second.pop("generated_at")
        self.assertEqual(first, second)

    def test_regime_rules_reproduce_engine_labels(self) -> None:
        high = explain_regime(
            "high_volatility",
            {"volatility_24h_pct": 2.0, "volatility_7d_pct": 1.0},
        )
        self.assertIn("volatility_24h_pct", {d["feature"] for d in high["drivers"]})
        self.assertEqual(high["driving_feature_group"], "engine")
        trending = explain_regime(
            "trending",
            {"volatility_24h_pct": 0.5, "volatility_7d_pct": 0.5, "momentum_24h_pct": 3.5},
        )
        self.assertIn("momentum_24h_pct", {d["feature"] for d in trending["drivers"]})

    def test_reproducibility_policy_block_is_present(self) -> None:
        reproducibility = build()["reproducibility"]
        self.assertTrue(reproducibility["deterministic"])
        self.assertEqual(reproducibility["dominant_model_weight_threshold"], 0.34)


class CliEmbeddingTests(unittest.TestCase):
    def test_forecast_json_embedding_via_cli_helper(self) -> None:
        engine_output = {
            "predictions": predictions(),
            "model_weights": weights(),
            "weighting_diagnostics": diagnostics(),
            "regime": "range",
            "market_features": features(),
        }
        section = btc_forecast.build_forecast_attribution_section(
            engine_output,
            abstention_policy=abstention(),
            dynamic_thresholds=thresholds(),
            direction_probability=probabilities(),
        )
        self.assertEqual(section["version"], FORECAST_ATTRIBUTION_VERSION)
        self.assertEqual(set(section["horizons"]), {"2h", "4h", "8h", "16h"})
        self.assertEqual(section["overall"]["state"], "edge")
        json.dumps(section)


if __name__ == "__main__":
    unittest.main()
