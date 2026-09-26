#!/usr/bin/env python3
"""Tests for leakage-safe, pre-registered feature interaction analysis."""

from __future__ import annotations

import json
import unittest

import numpy as np

from btc_timesfm.research.feature_interaction_analysis import (
    BASE_FEATURE_NAMES,
    INTERACTION_CATALOG,
    INTERACTION_SCHEMA_VERSION,
    build_interaction_analysis_report,
    catalog_sha256,
    catalog_signature,
    eligible_training_indices,
    evaluate_interaction,
    promotion_status_from_evidence,
)

EXPECTED_INTERACTION_IDS = {
    "funding_x_trend",
    "open_interest_x_volatility",
    "order_book_imbalance_x_momentum",
    "eth_relative_strength_x_btc_regime",
}


def _z(series: list[float]) -> np.ndarray:
    values = np.asarray(series, dtype=float)
    return (values - values.mean()) / (values.std() + 1e-9)


def _interaction(interaction_id: str) -> dict:
    return next(entry for entry in INTERACTION_CATALOG if entry["interaction_id"] == interaction_id)


def _base_frame(n: int, seed: int = 7) -> dict[str, list[object]]:
    rng = np.random.default_rng(seed)
    frame: dict[str, list[object]] = {
        "volatility_24h_pct": list(rng.uniform(0.5, 3.0, n)),
        "volatility_7d_pct": list(rng.uniform(0.6, 3.0, n)),
        "range_24h_avg_pct": list(rng.uniform(0.3, 2.0, n)),
        "volume_zscore_7d": list(rng.normal(0, 1, n)),
        "rsi_14": list(rng.uniform(30, 70, n)),
        "momentum_6h_pct": list(rng.normal(0, 1, n)),
        "momentum_24h_pct": list(rng.normal(0, 1, n)),
        "momentum_7d_pct": list(rng.normal(0, 1, n)),
        "derivatives_funding_rate_pct": list(rng.normal(0, 0.1, n)),
        "derivatives_open_interest_usd": list(1e9 + rng.normal(0, 5e7, n)),
        "derivatives_oi_change_1h_pct": list(rng.normal(0, 0.3, n)),
        "derivatives_oi_change_24h_pct": list(rng.normal(0, 2.0, n)),
        "microstructure_imbalance_10bps": list(rng.normal(0, 0.3, n)),
        "microstructure_imbalance_25bps": list(rng.normal(0, 0.3, n)),
        "microstructure_microprice_deviation_bps": list(rng.normal(0, 1.0, n)),
        "cross_eth_btc_relative_6h_pct": list(rng.normal(0, 1.0, n)),
        "cross_eth_btc_relative_24h_pct": list(rng.normal(0, 1.5, n)),
        "regime": ["trending" if index % 2 == 0 else "range" for index in range(n)],
    }
    return frame


def _trend_composite(frame: dict[str, list[object]]) -> np.ndarray:
    return _z(
        [
            sum(
                [
                    float(frame["momentum_6h_pct"][i]),
                    float(frame["momentum_24h_pct"][i]),
                    float(frame["momentum_7d_pct"][i]),
                ]
            )
            / 3.0
            for i in range(len(frame["derivatives_funding_rate_pct"]))
        ]
    )


def _funding_trend_target(
    frame: dict[str, list[object]], *, scale: float = 1.5, noise: float = 0.05, seed: int = 11
) -> list[float]:
    rng = np.random.default_rng(seed)
    n = len(frame["derivatives_funding_rate_pct"])
    funding = _z(frame["derivatives_funding_rate_pct"])
    product = funding * _trend_composite(frame)
    return list(scale * product + 0.05 * funding + noise * rng.normal(0, 1, n))


def _noise_target(frame: dict[str, list[object]], *, seed: int = 3) -> list[float]:
    rng = np.random.default_rng(seed)
    return list(rng.normal(0, 0.2, len(frame["regime"])))


def _folds(n: int, fold_count: int = 2) -> list[dict]:
    order = sorted(range(n), key=lambda index: index)
    folds = []
    for fold_index in range(fold_count):
        start = fold_index * n // fold_count
        end = (fold_index + 1) * n // fold_count
        if end > start:
            folds.append({"test": order[start:end]})
    return folds or [{"test": list(order)}]


class CatalogTests(unittest.TestCase):
    def test_catalog_is_explicit_versioned_and_bounded(self) -> None:
        required = (
            "interaction_id",
            "name",
            "description",
            "feature_a",
            "feature_b",
            "rationale",
            "rule",
        )
        self.assertEqual(INTERACTION_SCHEMA_VERSION, 1)
        self.assertEqual(
            {entry["interaction_id"] for entry in INTERACTION_CATALOG}, EXPECTED_INTERACTION_IDS
        )
        self.assertLessEqual(len(INTERACTION_CATALOG), 4)
        for entry in INTERACTION_CATALOG:
            for key in required:
                self.assertIn(key, entry)
            self.assertIn(entry["rule"]["type"], {"product", "regime_gate"})
            self.assertNotEqual(entry["feature_a"], entry["feature_b"])
        self.assertEqual(catalog_sha256(), catalog_sha256())
        signature = catalog_signature()
        self.assertEqual(signature["schema_version"], 1)
        self.assertEqual(signature["interaction_count"], len(INTERACTION_CATALOG))
        self.assertEqual(len(signature["catalog_sha256"]), 64)

    def test_fixed_catalog_avoids_exhaustive_search(self) -> None:
        n = 40
        frame = _base_frame(n)
        targets = {f"{h}h": _noise_target(frame) for h in (2, 4)}
        origin_indices = [index * 3600 for index in range(n)]
        report = build_interaction_analysis_report(
            frame,
            targets,
            origin_indices,
            _folds(n),
            interactions=[_interaction("funding_x_trend")],
            horizons=(2, 4),
            min_train=4,
        )
        self.assertEqual(list(report["interactions"]), ["funding_x_trend"])
        self.assertEqual(report["catalog_version"]["interaction_count"], 1)
        self.assertEqual(report["schema_version"], INTERACTION_SCHEMA_VERSION)


class LeakageSafetyTests(unittest.TestCase):
    def test_eligible_training_indices_exclude_future_targets(self) -> None:
        origin_indices = [index * 3600 for index in range(40)]
        eligible = eligible_training_indices(origin_indices, int(origin_indices[30]), 2)
        self.assertEqual(eligible, list(range(29)))
        for train_index in eligible:
            self.assertLessEqual(origin_indices[train_index] + 2 * 3600, origin_indices[30])

    def test_leaky_fold_raises_and_safe_fold_passes(self) -> None:
        n = 40
        frame = _base_frame(n)
        targets = {"2h": _noise_target(frame)}
        origin_indices = [index * 3600 for index in range(n)]
        interaction = _interaction("funding_x_trend")
        leaky = {"test": [30], "train": [31]}
        with self.assertRaisesRegex(ValueError, "leaks future targets"):
            evaluate_interaction(
                interaction, frame, targets, origin_indices, [leaky], horizons=(2,), min_train=1
            )
        safe_train = eligible_training_indices(origin_indices, int(origin_indices[30]), 2)
        report = evaluate_interaction(
            interaction,
            frame,
            targets,
            origin_indices,
            [{"test": [30], "train": safe_train}],
            horizons=(2,),
            min_train=1,
        )
        self.assertEqual(report["by_horizon"]["2h"]["walk_forward_samples"], 1)

    def test_report_is_deterministic_and_leakage_safe(self) -> None:
        n = 120
        frame = _base_frame(n)
        targets = {f"{h}h": _funding_trend_target(frame) for h in (2, 4)}
        origin_indices = [index * 3600 for index in range(n)]
        kwargs = {
            "features_frame": frame,
            "targets": targets,
            "origin_indices": origin_indices,
            "folds": _folds(n),
            "horizons": (2, 4),
            "min_train": 4,
            "generated_at": "2026-02-01T00:00:00+00:00",
        }
        first = build_interaction_analysis_report(**kwargs)
        second = build_interaction_analysis_report(**kwargs)
        self.assertEqual(first, second)
        self.assertTrue(first["fold_leakage_safe"])
        self.assertFalse(first["uses_future_information"])
        self.assertIn("leakage_safe", first["method"])
        for section in first["interactions"].values():
            self.assertEqual(section["overall"]["no_material_horizon_regression"], True)


class EvaluationReportsTests(unittest.TestCase):
    def test_incremental_delta_and_uncertainty_reported(self) -> None:
        n = 120
        frame = _base_frame(n)
        targets = {f"{h}h": _funding_trend_target(frame) for h in (2, 4)}
        origin_indices = [index * 3600 for index in range(n)]
        section = evaluate_interaction(
            _interaction("funding_x_trend"),
            frame,
            targets,
            origin_indices,
            _folds(n),
            horizons=(2, 4),
            min_train=4,
        )
        for horizon, item in section["by_horizon"].items():
            self.assertIn("walk_forward_samples", item)
            self.assertIsNotNone(item["incremental_delta_pp"], horizon)
            self.assertIsNotNone(item["incremental_relative_improvement"], horizon)
            self.assertIn("significance", item)
            ci = item["significance"]["improvement_ci"]
            self.assertIn("lower", ci)
            self.assertIn("upper", ci)
            self.assertIn("candidate_minus_baseline", item["significance"])
            self.assertGreater(item["walk_forward_samples"], 0, horizon)
            self.assertEqual(section["interaction_features"], ["interaction_funding_x_trend"])
            self.assertEqual(section["feature_sets"]["market_only"], list(BASE_FEATURE_NAMES))

    def test_low_sample_marked_inconclusive(self) -> None:
        n = 12
        frame = _base_frame(n)
        targets = {f"{h}h": _noise_target(frame) for h in (2, 4)}
        origin_indices = [index * 3600 for index in range(n)]
        empty = evaluate_interaction(
            _interaction("funding_x_trend"),
            frame,
            targets,
            origin_indices,
            _folds(n),
            horizons=(2, 4),
            min_train=100,
        )
        for horizon, item in empty["by_horizon"].items():
            self.assertEqual(item["walk_forward_samples"], 0, horizon)
            self.assertEqual(item["conclusion"], "inconclusive", horizon)
            self.assertEqual(item["promotion_status"], "insufficient_evidence", horizon)

        n = 60
        frame = _base_frame(n)
        targets = {f"{h}h": _noise_target(frame) for h in (2, 4)}
        origin_indices = [index * 3600 for index in range(n)]
        partial = evaluate_interaction(
            _interaction("funding_x_trend"),
            frame,
            targets,
            origin_indices,
            _folds(n),
            horizons=(2, 4),
            min_train=4,
            promotion_min_samples=200,
        )
        for horizon, item in partial["by_horizon"].items():
            self.assertGreater(item["walk_forward_samples"], 0, horizon)
            self.assertEqual(item["conclusion"], "inconclusive", horizon)
            self.assertEqual(item["promotion_status"], "insufficient_evidence", horizon)

    def test_report_is_json_serializable(self) -> None:
        n = 120
        frame = _base_frame(n)
        targets = {f"{h}h": _funding_trend_target(frame) for h in (2, 4)}
        origin_indices = [index * 3600 for index in range(n)]
        report = build_interaction_analysis_report(
            frame,
            targets,
            origin_indices,
            _folds(n),
            horizons=(2, 4),
            min_train=4,
            generated_at="2026-02-01T00:00:00+00:00",
        )
        payload = json.dumps(report, sort_keys=True)
        loaded = json.loads(payload)
        self.assertEqual(loaded, report)
        self.assertEqual(report["catalog_version"]["catalog_sha256"], catalog_sha256())
        self.assertEqual(
            report["interactions"]["funding_x_trend"]["overall"]["recommendation"],
            "insufficient_evidence",
        )


class PromotionTests(unittest.TestCase):
    def test_promotion_requires_stable_oos_evidence(self) -> None:
        self.assertEqual(
            promotion_status_from_evidence(
                conclusion="candidate_better",
                samples=50,
                fold_count=2,
                improvement_fraction=1.0,
                promotion_min_samples=32,
                min_stable_folds=2,
                effective_block_count=8,
            ),
            "promoted",
        )
        self.assertEqual(
            promotion_status_from_evidence(
                conclusion="candidate_better",
                samples=50,
                fold_count=2,
                improvement_fraction=0.5,
                promotion_min_samples=32,
                min_stable_folds=2,
                effective_block_count=8,
            ),
            "research_only",
        )
        self.assertEqual(
            promotion_status_from_evidence(
                conclusion="candidate_better",
                samples=5,
                fold_count=2,
                improvement_fraction=1.0,
                promotion_min_samples=32,
                min_stable_folds=2,
            ),
            "insufficient_evidence",
        )
        self.assertEqual(
            promotion_status_from_evidence(
                conclusion="baseline_better",
                samples=50,
                fold_count=2,
                improvement_fraction=0.0,
                promotion_min_samples=32,
                min_stable_folds=2,
                effective_block_count=8,
            ),
            "rejected",
        )
        self.assertEqual(
            promotion_status_from_evidence(
                conclusion="candidate_better",
                samples=50,
                fold_count=1,
                improvement_fraction=1.0,
                promotion_min_samples=32,
                min_stable_folds=2,
                effective_block_count=8,
            ),
            "research_only",
        )

    def test_underpowered_candidate_better_result_is_not_promoted(self) -> None:
        self.assertEqual(
            promotion_status_from_evidence(
                conclusion="candidate_better",
                samples=50,
                fold_count=2,
                improvement_fraction=1.0,
                promotion_min_samples=32,
                min_stable_folds=2,
            ),
            "insufficient_evidence",
        )

    def test_strong_interaction_is_promoted_end_to_end(self) -> None:
        n = 200
        frame = _base_frame(n)
        targets = {f"{h}h": _funding_trend_target(frame) for h in (2, 4)}
        origin_indices = [index * 3600 for index in range(n)]
        report = build_interaction_analysis_report(
            frame,
            targets,
            origin_indices,
            _folds(n),
            interactions=[_interaction("funding_x_trend")],
            horizons=(2, 4),
            min_train=8,
            promotion_min_samples=8,
        )
        section = report["interactions"]["funding_x_trend"]
        for horizon, item in section["by_horizon"].items():
            self.assertEqual(item["promotion_status"], "insufficient_evidence", horizon)
            self.assertTrue(item["fold_stats"]["stable_oos"], horizon)
        self.assertEqual(section["overall"]["promotion"], "insufficient_evidence")

    def test_noisy_interaction_is_not_promoted(self) -> None:
        n = 150
        frame = _base_frame(n)
        targets = {f"{h}h": _noise_target(frame) for h in (2, 4)}
        origin_indices = [index * 3600 for index in range(n)]
        section = evaluate_interaction(
            _interaction("funding_x_trend"),
            frame,
            targets,
            origin_indices,
            _folds(n),
            horizons=(2, 4),
            min_train=8,
        )
        for horizon, item in section["by_horizon"].items():
            self.assertNotEqual(item["promotion_status"], "promoted", horizon)
        self.assertNotEqual(section["overall"]["promotion"], "promoted")

    def test_regime_gate_interaction_runs_end_to_end(self) -> None:
        n = 120
        frame = _base_frame(n)
        targets = {f"{h}h": _funding_trend_target(frame) for h in (2, 4)}
        origin_indices = [index * 3600 for index in range(n)]
        section = evaluate_interaction(
            _interaction("eth_relative_strength_x_btc_regime"),
            frame,
            targets,
            origin_indices,
            _folds(n),
            horizons=(2, 4),
            min_train=4,
        )
        self.assertEqual(
            section["interaction_features"], ["interaction_eth_relative_strength_x_btc_regime"]
        )
        self.assertGreater(section["by_horizon"]["2h"]["walk_forward_samples"], 0)


if __name__ == "__main__":
    unittest.main()
