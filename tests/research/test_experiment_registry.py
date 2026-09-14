#!/usr/bin/env python3
"""Unit tests for the durable experiment registry and leaderboard."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from btc_timesfm.research.experiment_registry import (
    SCHEMA_VERSION,
    ExperimentRegistry,
    build_leaderboard,
    render_json,
    render_markdown,
)

HORIZONS = ("2h", "4h", "8h", "16h")


def _iso(hour: int) -> str:
    return f"2026-01-01T{hour:02d}:00:00+00:00"


def _metrics(
    objective_mae: float,
    by_horizon_mae: Mapping[str, float] | None = None,
    regime_mae: Mapping[str, float] | None = None,
    direction: float = 0.65,
    samples: int = 48,
) -> dict[str, Any]:
    by_horizon_mae = dict(by_horizon_mae) if by_horizon_mae else {}
    by_horizon: dict[str, Any] = {}
    for horizon in HORIZONS:
        mae = by_horizon_mae.get(horizon, objective_mae)
        by_horizon[horizon] = {
            "samples": samples,
            "mae_pct": mae,
            "mean_signed_error_pct": round(mae / 10.0, 6),
            "direction_accuracy": direction,
        }
    by_regime: dict[str, Any] = {}
    for regime, mae in (regime_mae or {}).items():
        by_regime[regime] = {
            horizon: {
                "samples": samples,
                "mae_pct": mae,
                "mean_signed_error_pct": round(mae / 10.0, 6),
                "direction_accuracy": direction,
            }
            for horizon in HORIZONS
        }
    return {
        "samples": samples,
        "objective_mae_pct": objective_mae,
        "mean_signed_error_pct": round(objective_mae / 10.0, 6),
        "mean_direction_accuracy": direction,
        "by_horizon": by_horizon,
        "by_regime": by_regime,
    }


def _register(
    registry: ExperimentRegistry,
    run_id: str,
    *,
    decision: str = "candidate",
    mae: float = 1.0,
    by_horizon_mae: Mapping[str, float] | None = None,
    regime_mae: Mapping[str, float] | None = None,
    created_at: str | None = None,
    run_type: str = "optimizer",
    **kwargs: Any,
) -> dict[str, Any]:
    metrics = _metrics(mae, by_horizon_mae, regime_mae)
    record, created = registry.register_experiment(
        run_id=run_id,
        run_type=run_type,
        created_at=created_at or _iso(int(run_id.count("a")) + 6),
        configuration_id=f"cfg-{run_id}",
        data_id=f"data-{run_id}",
        hypothesis="test hypothesis",
        feature_set_version="v7",
        model_set=["timesfm_168h", "persistence"],
        evaluation_window={
            "start": "2026-01-01T00:00:00+00:00",
            "end": "2026-01-31T00:00:00+00:00",
        },
        metrics=metrics,
        statistical_evidence={"conclusion": "inconclusive"},
        decision=decision,
        parent_run_id=None,
        git_sha="abc1234",
        report_path=f"reports/{run_id}.json",
        **kwargs,
    )
    assert created
    return record


class ExperimentRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "experiment_registry.sqlite"
        self.registry = ExperimentRegistry(self.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_schema_is_versioned_and_verifiable(self) -> None:
        verification = self.registry.verify()
        self.assertEqual(verification["schema_version"], SCHEMA_VERSION)
        self.assertEqual(verification["integrity"], "ok")
        stats = self.registry.stats()
        self.assertEqual(stats["schema_version"], SCHEMA_VERSION)
        self.assertEqual(stats["experiments"], 0)

    def test_register_and_duplicate_are_idempotent(self) -> None:
        first, created_first = self.registry.register_experiment(
            run_id="run-optimizer-1",
            run_type="optimizer",
            metrics=_metrics(0.9),
            decision="candidate",
        )
        self.assertTrue(created_first)
        second, created_second = self.registry.register_experiment(
            run_id="run-optimizer-1",
            run_type="optimizer",
            metrics=_metrics(5.0),
            decision="candidate",
        )
        self.assertFalse(created_second)
        self.assertEqual(second["run_id"], first["run_id"])
        self.assertEqual(second["metrics"], first["metrics"])
        self.assertEqual(self.registry.stats()["experiments"], 1)

    def test_register_from_manifest_extracts_identity(self) -> None:
        manifest = {
            "manifest_version": 1,
            "run_id": "optimizer-20260101T000000Z-abcdef12-1234abcd",
            "run_type": "optimizer",
            "created_at": "2026-01-01T00:00:00+00:00",
            "configuration_id": "cfg-sha-1234567890",
            "data_id": "data-0987654321",
            "code": {"git_sha": "deadbeef", "dirty": False},
            "configuration": {
                "feature_set_version": "v9",
                "forecast": {"model_names": ["timesfm_168h", "persistence"]},
            },
        }
        record, created = self.registry.register_from_manifest(
            manifest,
            metrics=_metrics(0.8),
        )
        self.assertTrue(created)
        self.assertEqual(record["run_id"], "optimizer-20260101T000000Z-abcdef12-1234abcd")
        self.assertEqual(record["configuration_id"], "cfg-sha-1234567890")
        self.assertEqual(record["data_id"], "data-0987654321")
        self.assertEqual(record["git_sha"], "deadbeef")
        self.assertEqual(record["feature_set_version"], "v9")
        self.assertEqual(record["model_set"], ["timesfm_168h", "persistence"])
        self.assertEqual(record["metrics"], _metrics(0.8))

    def test_champion_is_distinguished_from_candidates(self) -> None:
        champion_old = _register(self.registry, "run-prod-old", decision="champion", mae=1.0)
        _register(self.registry, "run-a", decision="candidate", mae=0.85)
        _register(self.registry, "run-b", decision="candidate", mae=0.95)
        champion_new = _register(self.registry, "run-prod-new", decision="champion", mae=1.05)

        board = self.registry.leaderboard()
        self.assertEqual(board["champion"]["run_id"], champion_new["run_id"])
        self.assertTrue(board["champion"]["is_champion"])
        self.assertEqual(board["champion"]["role"], "champion")
        self.assertNotEqual(board["champion"]["run_id"], champion_old["run_id"])
        candidate_ids = [entry["run_id"] for entry in board["candidates"]]
        self.assertEqual(candidate_ids, ["run-a", "run-b"])
        for entry in board["candidates"]:
            self.assertFalse(entry["is_champion"])
            self.assertEqual(entry["role"], "research")
        self.assertEqual(board["statistics"]["champion"], 2)
        self.assertEqual(board["statistics"]["candidates"], 2)

    def test_leaderboard_sorts_candidates_by_objective_mae(self) -> None:
        _register(self.registry, "run-prod", decision="champion", mae=1.0)
        _register(self.registry, "run-worst", mae=1.4)
        _register(self.registry, "run-mid", mae=1.1)
        _register(self.registry, "run-best", mae=0.7)

        board = self.registry.leaderboard()
        self.assertEqual(
            [entry["run_id"] for entry in board["candidates"]],
            ["run-best", "run-mid", "run-worst"],
        )
        maes = [entry["metric_summary"]["mae_pct"] for entry in board["candidates"]]
        self.assertEqual(maes, sorted(maes))

    def test_horizon_filter_ranks_by_that_horizon_metric(self) -> None:
        _register(self.registry, "run-prod", decision="champion", mae=1.0)
        _register(
            self.registry,
            "run-slow",
            mae=2.0,
            by_horizon_mae={"2h": 0.9, "4h": 2.0, "8h": 2.5, "16h": 2.6},
        )
        _register(
            self.registry,
            "run-fast",
            mae=2.1,
            by_horizon_mae={"2h": 0.5, "4h": 2.4, "8h": 2.4, "16h": 3.1},
        )
        _register(
            self.registry,
            "run-mid",
            mae=2.2,
            by_horizon_mae={"2h": 0.7, "4h": 2.5, "8h": 2.2, "16h": 3.4},
        )

        board = self.registry.leaderboard(horizon="2h")
        self.assertEqual(
            [entry["run_id"] for entry in board["candidates"]],
            ["run-fast", "run-mid", "run-slow"],
        )
        for entry in board["candidates"]:
            self.assertEqual(entry["metric_summary"]["mae_pct"], entry["metric"]["mae_pct"])
        self.assertEqual(board["filters"]["horizon"], "2h")
        self.assertEqual(board["statistics"]["candidates"], 3)

    def test_regime_filter_aggregates_regime_metrics(self) -> None:
        _register(self.registry, "run-best", mae=1.0, regime_mae={"range": 0.6, "trend": 1.2})
        _register(self.registry, "run-worst", mae=1.1, regime_mae={"range": 0.9, "trend": 1.0})

        board = self.registry.leaderboard(regime="range")
        self.assertEqual(
            [entry["run_id"] for entry in board["candidates"]], ["run-best", "run-worst"]
        )
        self.assertAlmostEqual(board["candidates"][0]["metric_summary"]["mae_pct"], 0.6)
        self.assertAlmostEqual(board["candidates"][1]["metric_summary"]["mae_pct"], 0.9)
        self.assertEqual(board["filters"]["regime"], "range")

    def test_regime_and_horizon_filters_combine(self) -> None:
        _register(
            self.registry,
            "run-a",
            mae=1.0,
            regime_mae={"range": 0.6, "trend": 1.5},
        )
        _register(
            self.registry,
            "run-b",
            mae=1.0,
            regime_mae={"range": 1.1, "trend": 1.2},
        )
        board = self.registry.leaderboard(horizon="16h", regime="range")
        self.assertEqual([entry["run_id"] for entry in board["candidates"]], ["run-a", "run-b"])
        self.assertAlmostEqual(board["candidates"][0]["metric_summary"]["mae_pct"], 0.6)
        self.assertAlmostEqual(board["candidates"][1]["metric_summary"]["mae_pct"], 1.1)

    def test_status_filter_separates_champion_from_candidates(self) -> None:
        _register(self.registry, "run-prod", decision="champion", mae=1.0)
        _register(self.registry, "run-candidate", mae=0.8)

        only_candidates = self.registry.leaderboard(status="candidate")
        self.assertIsNone(only_candidates["champion"])
        self.assertEqual(
            [entry["run_id"] for entry in only_candidates["candidates"]], ["run-candidate"]
        )

        only_champion = self.registry.leaderboard(status="champion")
        self.assertEqual(only_champion["champion"]["run_id"], "run-prod")
        self.assertEqual(only_champion["candidates"], [])

    def test_leaderboard_hash_is_reproducible_and_sensitive(self) -> None:
        _register(self.registry, "run-prod", decision="champion", mae=1.0)
        _register(self.registry, "run-a", mae=0.8)
        first = self.registry.leaderboard()
        second = self.registry.leaderboard()
        self.assertEqual(first["audit"]["sha256"], second["audit"]["sha256"])
        self.assertEqual(first["entries"], second["entries"])

        _register(self.registry, "run-b", mae=0.9)
        third = self.registry.leaderboard()
        self.assertNotEqual(third["audit"]["sha256"], first["audit"]["sha256"])

    def test_storage_stays_bounded_on_duplicate_run_ids(self) -> None:
        for _ in range(5):
            self.registry.register_experiment(
                run_id="run-dupe", run_type="optimizer", metrics=_metrics(1.0)
            )
        self.assertEqual(self.registry.stats()["experiments"], 1)

        self.registry.register_experiment(
            run_id="run-other", run_type="optimizer", metrics=_metrics(1.1)
        )
        self.assertEqual(self.registry.stats()["experiments"], 2)
        for _ in range(5):
            self.registry.register_experiment(
                run_id="run-dupe", run_type="optimizer", metrics=_metrics(1.0)
            )
        self.assertEqual(self.registry.stats()["experiments"], 2)

    def test_decision_updates_are_recorded_and_audited(self) -> None:
        self.registry.register_experiment(
            run_id="run-a", run_type="optimizer", metrics=_metrics(0.8)
        )
        before = self.registry.get("run-a")
        self.assertEqual(before["decision"], "candidate")

        promoted = self.registry.update_decision("run-a", "champion")
        self.assertEqual(promoted["decision"], "champion")
        self.assertEqual(self.registry.get("run-a")["decision"], "champion")
        self.assertEqual(self.registry.stats()["decision_updates"], 1)

        rejected = self.registry.update_decision("run-a", "rejected")
        self.assertEqual(rejected["decision"], "rejected")
        self.assertEqual(self.registry.stats()["decision_updates"], 2)

        with self.assertRaises(ValueError):
            self.registry.update_decision("run-a", "not-a-decision")
        with self.assertRaises(KeyError):
            self.registry.update_decision("run-missing", "candidate")

    def test_outputs_are_json_serializable(self) -> None:
        _register(self.registry, "run-prod", decision="champion", mae=1.0)
        _register(self.registry, "run-a", mae=0.8)
        board = self.registry.leaderboard(horizon="2h")

        encoded = json.dumps(board, sort_keys=True)
        self.assertIn('"is_champion": true', encoded)

        restored = json.loads(render_json(board))
        self.assertEqual(restored["audit"], board["audit"])

        markdown = render_markdown(board)
        self.assertIn("# Experiment leaderboard", markdown)
        self.assertIn("CHAMPION", markdown)
        self.assertIn("Research candidates", markdown)
        self.assertIn(board["audit"]["sha256"], markdown)

    def test_build_leaderboard_accepts_path_and_instance(self) -> None:
        _register(self.registry, "run-a", mae=0.8)
        path_board = build_leaderboard(self.db_path)
        instance_board = build_leaderboard(self.registry)
        self.assertEqual(path_board["audit"], instance_board["audit"])

    def test_metrics_round_trip_preserves_by_horizon_and_bias(self) -> None:
        metrics = _metrics(
            1.0,
            by_horizon_mae={"2h": 0.4, "16h": 1.8},
            regime_mae={"range": 0.7},
            direction=0.71,
        )
        record, _ = self.registry.register_experiment(
            run_id="run-detailed",
            run_type="optimizer",
            created_at=_iso(12),
            metrics=metrics,
        )
        self.assertEqual(record["metrics"], metrics)
        self.assertEqual(record["metrics"]["by_horizon"]["2h"]["direction_accuracy"], 0.71)
        created_at = datetime.fromisoformat(record["created_at"])
        self.assertEqual(created_at.tzinfo, timezone.utc)


if __name__ == "__main__":
    unittest.main()
