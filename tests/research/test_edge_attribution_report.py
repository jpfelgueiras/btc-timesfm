import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from btc_timesfm.history.history_store import ForecastHistoryStore
from btc_timesfm.research.edge_attribution_report import (
    build_report,
    generate_report,
    render_markdown,
)


NOW = datetime(2026, 9, 5, 22, 0, tzinfo=timezone.utc)


def row(
    *,
    origin_at: str,
    model: str,
    horizon: int,
    target_at: str,
    mae: float,
    direction: int,
    bias: float = 0.0,
    regime: str = "range",
    features: dict[str, object] | None = None,
    agreement: float | None = 0.8,
    weight: float | None = None,
    manifest: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "origin_at": origin_at,
        "target_at": target_at,
        "model_name": model,
        "horizon_hours": horizon,
        "regime": regime,
        "market_features_json": json.dumps(features or {}),
        "experiment_manifest_json": json.dumps(manifest or {}),
        "configuration_id": "cfg-test",
        "actual_target_price_usd": 100.0,
        "absolute_error_pct": mae,
        "signed_error_pct": bias,
        "direction_correct": direction,
        "model_agreement": agreement,
        "ensemble_weight": weight,
    }


class EdgeAttributionReportTests(unittest.TestCase):
    def test_pairs_ensemble_against_persistence_by_horizon(self) -> None:
        rows = [
            row(
                origin_at="2026-09-05T18:00:00+00:00",
                target_at="2026-09-05T20:00:00+00:00",
                model="ensemble",
                horizon=2,
                mae=1.0,
                direction=1,
                features={"volatility_24h_pct": 0.5, "momentum_24h_pct": 0.2},
            ),
            row(
                origin_at="2026-09-05T18:00:00+00:00",
                target_at="2026-09-05T20:00:00+00:00",
                model="persistence",
                horizon=2,
                mae=3.0,
                direction=0,
            ),
            row(
                origin_at="2026-09-05T18:00:00+00:00",
                target_at="2026-09-05T20:00:00+00:00",
                model="timesfm_168",
                horizon=2,
                mae=2.0,
                direction=1,
                weight=0.7,
            ),
        ]

        report = build_report(
            rows,
            now=NOW,
            low_sample_threshold=1,
            min_paired_samples=1,
            bootstrap_iterations=100,
        )

        self.assertEqual(report["paired_samples"], 1)
        two_hour = report["by_dimension"]["horizon"]["2h"]
        self.assertEqual(two_hour["ensemble_mae_pct"], 1.0)
        self.assertEqual(two_hour["persistence_mae_pct"], 3.0)
        self.assertEqual(two_hour["mae_delta_pct_points"], 2.0)
        self.assertEqual(two_hour["direction_accuracy_delta"], 1.0)
        self.assertIn("top:timesfm_168", report["by_dimension"]["model_contribution"])

    def test_required_horizons_and_low_sample_segments_are_marked(self) -> None:
        report = build_report([], now=NOW, low_sample_threshold=2, bootstrap_iterations=100)

        self.assertEqual(report["horizons"], ["2h", "4h", "8h", "16h"])
        for horizon in report["horizons"]:
            segment = report["by_dimension"]["horizon"][horizon]
            self.assertEqual(segment["reason"], "no_paired_samples")
            self.assertTrue(segment["unstable_or_low_sample"])

    def test_segments_include_regime_volatility_time_feature_confidence(self) -> None:
        manifest = {"configuration": {"feature_set_version": "features-v2"}}
        rows = []
        for index in range(4):
            origin = f"2026-09-0{index + 1}T13:00:00+00:00"
            target = f"2026-09-0{index + 1}T17:00:00+00:00"
            rows.extend(
                [
                    row(
                        origin_at=origin,
                        target_at=target,
                        model="ensemble",
                        horizon=4,
                        mae=1.0,
                        direction=1,
                        regime="high_volatility",
                        features={
                            "volatility_24h_pct": 4.0,
                            "momentum_24h_pct": 4.5,
                            "hour_utc": 13,
                            "weekday_utc": index,
                        },
                        agreement=0.9,
                        manifest=manifest,
                    ),
                    row(
                        origin_at=origin,
                        target_at=target,
                        model="persistence",
                        horizon=4,
                        mae=2.0,
                        direction=0,
                    ),
                ]
            )

        report = build_report(
            rows,
            now=NOW,
            low_sample_threshold=3,
            min_paired_samples=3,
            bootstrap_iterations=100,
        )

        self.assertIn("high_volatility", report["by_dimension"]["regime"])
        self.assertIn("high", report["by_dimension"]["volatility_bucket"])
        self.assertIn("high", report["by_dimension"]["trend_strength"])
        self.assertIn("12-18", report["by_dimension"]["time_of_day"])
        self.assertIn("mon", report["by_dimension"]["day_of_week"])
        self.assertIn("high", report["by_dimension"]["confidence_bucket"])
        self.assertIn("features-v2", report["by_dimension"]["feature_set_version"])

    def test_markdown_surfaces_ci_and_unstable(self) -> None:
        report = build_report([], now=NOW, bootstrap_iterations=100)
        markdown = render_markdown(report)

        self.assertIn("Edge attribution report", markdown)
        self.assertIn("95% CI", markdown)
        self.assertIn("True", markdown)
        self.assertIn("persistence", markdown.lower())

    def test_generate_report_reads_durable_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "history.sqlite"
            store = ForecastHistoryStore(db)
            snapshot = {
                "generated_at": "2026-09-05T18:00:00+00:00",
                "latest_close_at": "2026-09-05T18:00:00+00:00",
                "latest_close_usd": 100.0,
                "source": "test",
                "pair": "BTCUSD",
                "regime": "range",
                "market_features": {"volatility_24h_pct": 0.5, "momentum_24h_pct": 0.2},
                "predictions": {"2h": {"price_usd": 102.0, "change_pct": 2.0}},
                "model_predictions": {"persistence": {"2h": {"price_usd": 100.0}}},
                "model_weights": {"2h": {"persistence": 0.2}},
                "experiment_manifest": {
                    "run_id": "run-test",
                    "configuration_id": "cfg-test",
                    "configuration": {"feature_set_version": "features-v2"},
                },
            }
            target = int(datetime(2026, 9, 5, 20, tzinfo=timezone.utc).timestamp())
            store.ingest_snapshot(snapshot, {target: 101.0})

            report = generate_report(
                db,
                json_path=root / "edge.json",
                markdown_path=root / "edge.md",
                low_sample_threshold=1,
                min_paired_samples=1,
                bootstrap_iterations=100,
            )

            self.assertEqual(report["paired_samples"], 1)
            self.assertTrue((root / "edge.json").exists())
            self.assertTrue((root / "edge.md").exists())

    def test_invalid_configuration_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_report([], low_sample_threshold=0)
        with self.assertRaises(ValueError):
            build_report([], min_paired_samples=0)
        with self.assertRaises(ValueError):
            build_report([], bootstrap_iterations=99)


if __name__ == "__main__":
    unittest.main()
