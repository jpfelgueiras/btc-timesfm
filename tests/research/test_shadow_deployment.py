#!/usr/bin/env python3
"""Unit tests for shadow deployment of research challengers."""

from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from btc_timesfm.research.shadow_deployment import (
    ShadowPolicy,
    ShadowStore,
    _evaluate_series,
    build_shadow_status,
    render_summary,
    run_shadow,
    shadow_policy_identity,
)

HORIZONS = ("2h", "4h", "8h", "16h")
HOURS = (2, 4, 8, 16)
BASE_EPOCH = 1767264000  # 2026-01-01T12:00:00+00:00


def _origin_timestamp(index: int) -> int:
    return BASE_EPOCH + index * 86400


def _iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _model_predictions(current_price: float) -> dict[str, Any]:
    return {
        "persistence": {horizon: {"price_usd": current_price} for horizon in HORIZONS},
        "ar1": {horizon: {"price_usd": round(current_price * 1.05, 2)} for horizon in HORIZONS},
    }


def _production_predictions(current_price: float, offset: float) -> dict[str, Any]:
    predictions: dict[str, Any] = {}
    for hour in HOURS:
        price = current_price + offset
        predictions[f"{hour}h"] = {
            "price_usd": round(price, 2),
            "change_pct": round((price / current_price - 1.0) * 100.0, 4),
            "q10_usd": round(current_price * 0.98, 2),
            "q50_usd": round(price, 2),
            "q90_usd": round(current_price * 1.02, 2),
            "model_agreement": 0.5,
        }
    return predictions


def _production_snapshot(index: int, *, price: float, offset: float = 6.0) -> dict[str, Any]:
    origin_at = _iso(_origin_timestamp(index))
    return {
        "generated_at": origin_at,
        "experiment_manifest": {
            "data_id": f"data-test-{index}",
            "data": {"latest_close_at": origin_at},
        },
        "latest_close_at": origin_at,
        "latest_close_usd": price,
        "regime": "range",
        "model_weights": {horizon: {"persistence": 0.5, "ar1": 0.5} for horizon in HORIZONS},
        "model_predictions": _model_predictions(price),
        "predictions": _production_predictions(price, offset),
    }


def _actuals_for(count: int) -> dict[int, float]:
    actuals: dict[int, float] = {}
    for index in range(count):
        timestamp = _origin_timestamp(index)
        close = 100.0 + index * 0.5
        actuals[timestamp] = close
        for hour in HOURS:
            target = timestamp + hour * 3600
            actuals[target] = close + 0.1 * hour
    return actuals


def _record_static(
    store: ShadowStore,
    configuration_id: str,
    index: int,
    *,
    price_by_horizon: dict[str, float],
) -> None:
    timestamp = _origin_timestamp(index)
    origin_at = _iso(timestamp)
    predictions = {
        horizon: {"price_usd": round(price, 2)} for horizon, price in price_by_horizon.items()
    }
    store.record_forecast(
        configuration_id=configuration_id,
        origin_at=origin_at,
        latest_close_at=origin_at,
        latest_close_usd=round(100.0 + index * 0.5, 2),
        regime="range",
        model_predictions={},
        model_weights={},
        predictions=predictions,
        forecast_sha256="static-test-sha",
        data_lineage_id=f"data-test-{index}",
    )


class ShadowDeploymentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "shadow_deployment.sqlite"
        self.store = ShadowStore(self.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _register_champion(self) -> dict[str, Any]:
        record, _ = self.store.register_configuration(
            name="production", parameters={}, role="champion"
        )
        return record

    def _register_challenger(
        self, *, name: str = "shadow_challenger", approved: bool = True
    ) -> dict[str, Any]:
        record, _ = self.store.register_configuration(
            name=name,
            parameters={"history_limit": 300},
            role="challenger",
            approval_status="approved" if approved else "pending",
        )
        return record

    def _seed_evaluation_origins(self, count: int, *, challenger_mae: float = 0.0) -> None:
        champion = self._register_champion()
        challenger = self._register_challenger()
        for index in range(count):
            close = 100.0 + index * 0.5
            champion_prices = {horizon: close + 8.0 for horizon in HORIZONS}
            challenger_prices = {
                horizon: close + 0.1 * hour + challenger_mae
                for horizon, hour in zip(HORIZONS, HOURS)
            }
            _record_static(
                self.store, champion["configuration_id"], index, price_by_horizon=champion_prices
            )
            _record_static(
                self.store,
                challenger["configuration_id"],
                index,
                price_by_horizon=challenger_prices,
            )
        self.store.mature_outcomes(_actuals_for(count))

    # --- shadow run isolation ------------------------------------------------

    def test_shadow_run_does_not_change_public_output_and_is_idempotent(self) -> None:
        configs = [{"name": "longer_history", "parameters": {"history_limit": 300}}]
        production = _production_snapshot(0, price=100.0)
        before = copy.deepcopy(production)
        actuals = _actuals_for(3)

        first = run_shadow(self.store, production, actuals, configs=configs)
        self.assertFalse(first["production_output_changed"])
        self.assertFalse(first["production_parameters_touched"])
        self.assertEqual(production, before)

        champion_id = first["shadow_champion"]["configuration_id"]
        champion_rows = self.store.load_forecasts(champion_id)
        self.assertEqual(len(champion_rows), 1)
        self.assertEqual(champion_rows[0]["predictions"], production["predictions"])

        challenger_id = first["challengers"][0]["configuration_id"]
        self.assertNotEqual(challenger_id, champion_id)
        self.assertTrue(first["challengers"][0]["challenger_persisted"])

        second = run_shadow(self.store, production, actuals, configs=configs)
        self.assertEqual(second["shadow_champion"]["persisted"], False)
        self.assertEqual(second["challengers"][0]["challenger_persisted"], False)
        self.assertEqual(self.store.count_forecasts(), 2)

    def test_shadow_forecasts_are_persisted_separately_per_configuration(self) -> None:
        challenger = self._register_challenger(name="longer_history")
        for index in range(2):
            run_shadow(
                self.store,
                _production_snapshot(index, price=100.0 + index * 0.5),
                _actuals_for(index + 1),
                configs=[
                    {
                        "name": "longer_history",
                        "parameters": {"history_limit": 300},
                    }
                ],
            )
        champion_id = self.store.champion_configuration()["configuration_id"]
        self.assertEqual(self.store.count_forecasts(champion_id), 2)
        self.assertEqual(self.store.count_forecasts(challenger["configuration_id"]), 2)
        champion_origins = {row["origin_at"] for row in self.store.load_forecasts(champion_id)}
        challenger_origins = {
            row["origin_at"] for row in self.store.load_forecasts(challenger["configuration_id"])
        }
        self.assertEqual(champion_origins, challenger_origins)
        self.assertEqual(
            self.store.load_forecasts(champion_id)[0]["data_lineage_id"],
            self.store.load_forecasts(challenger["configuration_id"])[0]["data_lineage_id"],
        )
        self.assertNotEqual(
            self.store.load_forecasts(champion_id)[0]["predictions"],
            self.store.load_forecasts(challenger["configuration_id"])[0]["predictions"],
        )

    def test_unapproved_challengers_do_not_run(self) -> None:
        pending = self._register_challenger(name="not_approved", approved=False)
        result = run_shadow(self.store, _production_snapshot(0, price=100.0), _actuals_for(1))
        self.assertEqual(result["challengers"], [])
        self.assertEqual(self.store.count_forecasts(pending["configuration_id"]), 0)

    def test_shadow_run_requires_public_data_lineage(self) -> None:
        production = _production_snapshot(0, price=100.0)
        del production["experiment_manifest"]
        with self.assertRaisesRegex(ValueError, "experiment_manifest.data_id"):
            run_shadow(self.store, production, _actuals_for(1))

    def test_challenger_failure_is_observable_without_stopping_champion(self) -> None:
        result = run_shadow(
            self.store,
            _production_snapshot(0, price=100.0),
            _actuals_for(1),
            configs=[{"name": "invalid", "parameters": {"enabled_models": ["ar1"]}}],
        )
        self.assertTrue(result["shadow_champion"]["persisted"])
        self.assertEqual(result["challengers"][0]["status"], "failed")
        self.assertEqual(len(self.store.load_failures()), 1)
        self.assertEqual(self.store.stats()["observable_failures"], 1)

    # --- evaluation ---------------------------------------------------------

    def test_evaluation_compares_champion_and_persistence_on_identical_origins(self) -> None:
        challenger = self._register_challenger()
        self._seed_evaluation_origins(35)
        evaluation = self.store.evaluate(challenger["configuration_id"])

        self.assertEqual(evaluation["significance"]["identical_origins"], 35)
        self.assertEqual(evaluation["maturity"]["common_origin_count"], 35)
        self.assertTrue(evaluation["maturity"]["identical_data_lineage"])
        self.assertEqual(evaluation["maturity"]["lineage_mismatch_count"], 0)
        self.assertEqual(evaluation["maturity"]["live_samples"], 35)
        self.assertEqual(evaluation["maturity"]["fully_matured_samples"], 35)
        self.assertEqual(evaluation["metrics"]["champion"]["samples"], 35)
        self.assertEqual(evaluation["metrics"]["challenger"]["samples"], 35)
        self.assertEqual(evaluation["metrics"]["persistence"]["samples"], 35)

        champion_conclusion = evaluation["significance"]["vs_champion"]["objective"]["conclusion"]
        persistence_conclusion = evaluation["significance"]["vs_persistence"]["objective"][
            "conclusion"
        ]
        self.assertEqual(champion_conclusion, "inconclusive")
        self.assertEqual(persistence_conclusion, "inconclusive")
        self.assertEqual(
            evaluation["significance"]["vs_champion"]["objective"]["reason"],
            "insufficient_effective_samples",
        )
        self.assertEqual(
            evaluation["significance"]["vs_champion"]["objective"]["effective_block_count_proxy"],
            round(35 / 24, 6),
        )

    def test_evaluation_is_deterministic(self) -> None:
        challenger = self._register_challenger()
        self._seed_evaluation_origins(35)
        first = self.store.evaluate(challenger["configuration_id"])
        second = self.store.evaluate(challenger["configuration_id"])
        self.assertEqual(first["significance"], second["significance"])
        self.assertEqual(first["metrics"], second["metrics"])

    # --- configurable requirements ------------------------------------------

    def test_minimum_samples_and_observation_window_are_configurable(self) -> None:
        challenger = self._register_challenger()
        self._seed_evaluation_origins(35)

        strict = self.store.evaluate(
            challenger["configuration_id"],
            policy=ShadowPolicy(minimum_live_samples=40, observation_window_days=30),
        )
        self.assertFalse(strict["maturity"]["checks"]["enough_live_samples"])
        self.assertEqual(strict["promotion"]["decision"], "blocked")

        long_window = self.store.evaluate(
            challenger["configuration_id"],
            policy=ShadowPolicy(minimum_live_samples=32, observation_window_days=60),
        )
        self.assertFalse(long_window["maturity"]["checks"]["observation_window_met"])
        self.assertEqual(long_window["promotion"]["decision"], "blocked")

        lenient = self.store.evaluate(
            challenger["configuration_id"],
            policy=ShadowPolicy(
                minimum_live_samples=32,
                minimum_paired_samples_per_horizon=32,
                observation_window_days=30,
            ),
        )
        self.assertTrue(lenient["maturity"]["checks"]["enough_live_samples"])
        self.assertTrue(lenient["maturity"]["checks"]["enough_paired_samples_per_horizon"])
        self.assertTrue(lenient["maturity"]["checks"]["observation_window_met"])
        self.assertEqual(lenient["promotion"]["decision"], "blocked")
        self.assertFalse(lenient["promotion"]["checks"]["enough_effective_blocks_vs_champion"])

    def test_promotion_requires_minimum_pairs_at_each_horizon(self) -> None:
        challenger = self._register_challenger()
        self._seed_evaluation_origins(35)
        evaluation = self.store.evaluate(
            challenger["configuration_id"],
            policy=ShadowPolicy(
                minimum_live_samples=1,
                minimum_paired_samples_per_horizon=200,
                observation_window_days=1,
            ),
        )
        self.assertEqual(
            evaluation["maturity"]["paired_samples_by_horizon"],
            {horizon: 35 for horizon in HORIZONS},
        )
        self.assertFalse(evaluation["maturity"]["checks"]["enough_paired_samples_per_horizon"])
        self.assertEqual(evaluation["promotion"]["decision"], "blocked")

    def test_promotion_blocked_until_requirements_met(self) -> None:
        challenger = self._register_challenger()
        self._seed_evaluation_origins(30, challenger_mae=0.0)
        evaluation = self.store.evaluate(
            challenger["configuration_id"],
            policy=ShadowPolicy(minimum_live_samples=32, observation_window_days=30),
        )
        self.assertFalse(evaluation["maturity"]["checks"]["enough_live_samples"])
        self.assertFalse(evaluation["maturity"]["checks"]["observation_window_met"])
        self.assertEqual(evaluation["promotion"]["decision"], "blocked")
        self.assertIn("requirement_not_met:enough_live_samples", evaluation["promotion"]["reasons"])
        self.assertIn(
            "requirement_not_met:observation_window_met", evaluation["promotion"]["reasons"]
        )

        weaker = self.store.evaluate(
            challenger["configuration_id"],
            policy=ShadowPolicy(
                minimum_live_samples=25,
                minimum_paired_samples_per_horizon=25,
                observation_window_days=20,
            ),
        )
        self.assertEqual(weaker["promotion"]["decision"], "blocked")
        self.assertIn(
            "requirement_not_met:enough_effective_blocks_vs_champion",
            weaker["promotion"]["reasons"],
        )

    def test_evaluation_pairs_maturity_separately_per_horizon(self) -> None:
        origin = _iso(_origin_timestamp(0))
        forecasts = {
            origin: {
                "latest_close_usd": 100.0,
                "predictions": {horizon: {"price_usd": 100.0} for horizon in HORIZONS},
            }
        }
        outcomes = {
            origin: {
                "2h": {
                    "actual_at": _iso(_origin_timestamp(0) + 2 * 3600),
                    "actual_price_usd": 101.0,
                },
                "4h": {
                    "actual_at": _iso(_origin_timestamp(0) + 4 * 3600),
                    "actual_price_usd": 102.0,
                },
            }
        }
        candidate_outcomes = {
            origin: {
                **outcomes[origin],
                # A mismatched target is not a paired 2h maturity.
                "2h": {
                    "actual_at": _iso(_origin_timestamp(0) + 3 * 3600),
                    "actual_price_usd": 101.0,
                },
            }
        }
        _, significance, _, _, paired = _evaluate_series(
            common_origins=[origin],
            challenger_forecasts=forecasts,
            champion_forecasts=forecasts,
            challenger_outcomes=candidate_outcomes,
            champion_outcomes=outcomes,
            policy=ShadowPolicy(minimum_live_samples=1, minimum_paired_samples_per_horizon=1),
        )
        self.assertEqual(paired["2h"], 0)
        self.assertEqual(paired["4h"], 1)
        self.assertEqual(significance["paired_samples_by_horizon"], paired)

    def test_requirements_are_reported_in_summary(self) -> None:
        self._register_challenger(name="report_runner")
        self._seed_evaluation_origins(30)
        policy = ShadowPolicy(minimum_live_samples=32, observation_window_days=30)
        status = build_shadow_status(self.store, policy=policy)
        self.assertEqual(status["policy_id"], shadow_policy_identity(policy))
        self.assertEqual(status["challengers"][0]["challenger"]["name"], "report_runner")
        summary = render_summary(status)
        self.assertIn("# Shadow deployment", summary)
        self.assertIn("blocked", summary)
        self.assertIn(status["policy_id"], summary)

    # --- maturation idempotency ---------------------------------------------

    def test_maturation_is_idempotent(self) -> None:
        champion = self._register_champion()
        challenger = self._register_challenger()
        close = 100.0
        champion_prices = {horizon: close + 8.0 for horizon in HORIZONS}
        challenger_prices = {horizon: close + 0.1 * hour for horizon, hour in zip(HORIZONS, HOURS)}
        _record_static(
            self.store, champion["configuration_id"], 0, price_by_horizon=champion_prices
        )
        _record_static(
            self.store,
            challenger["configuration_id"],
            0,
            price_by_horizon=challenger_prices,
        )
        actuals = _actuals_for(1)
        first = self.store.mature_outcomes(actuals)
        self.assertEqual(first["inserted_outcomes"], 8)
        self.assertEqual(self.store.count_outcomes(), 8)

        second = self.store.mature_outcomes(actuals)
        self.assertEqual(second["inserted_outcomes"], 0)
        self.assertEqual(second["already_matured"], 8)
        self.assertEqual(self.store.count_outcomes(), 8)

    def test_policy_identity_is_stable_and_sensitive(self) -> None:
        first = shadow_policy_identity(ShadowPolicy())
        self.assertEqual(first, shadow_policy_identity(ShadowPolicy()))
        self.assertNotEqual(
            first,
            shadow_policy_identity(ShadowPolicy(minimum_live_samples=48)),
        )


if __name__ == "__main__":
    unittest.main()
