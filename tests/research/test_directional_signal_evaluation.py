import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from btc_timesfm.history.history_store import ForecastHistoryStore
from btc_timesfm.research.directional_signal_evaluation import (
    build_report,
    classify_direction,
    generate_report,
    render_markdown,
)


NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def row(
    *,
    origin_at: str,
    horizon: int,
    predicted: float,
    actual: float,
    model: str = "ensemble",
    regime: str = "range",
    features: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "origin_at": origin_at,
        "target_at": origin_at,
        "model_name": model,
        "horizon_hours": horizon,
        "regime": regime,
        "market_features_json": json.dumps(features or {}),
        "actual_target_price_usd": 100.0,
        "predicted_change_pct": predicted,
        "actual_change_pct": actual,
    }


class DirectionalSignalEvaluationTests(unittest.TestCase):
    def test_classify_direction_uses_configurable_neutral_threshold(self) -> None:
        self.assertEqual(classify_direction(0.3, 0.25), "up")
        self.assertEqual(classify_direction(-0.3, 0.25), "down")
        self.assertEqual(classify_direction(0.2, 0.25), "neutral")
        with self.assertRaises(ValueError):
            classify_direction(0.0, -0.1)

    def test_metrics_by_horizon_separate_direction_from_magnitude(self) -> None:
        rows = [
            row(origin_at="2026-09-06T00:00:00+00:00", horizon=2, predicted=1.0, actual=2.0),
            row(origin_at="2026-09-06T02:00:00+00:00", horizon=2, predicted=-1.0, actual=1.0),
            row(origin_at="2026-09-06T04:00:00+00:00", horizon=4, predicted=0.1, actual=0.2),
            row(
                origin_at="2026-09-06T06:00:00+00:00",
                horizon=2,
                predicted=9.0,
                actual=9.0,
                model="persistence",
            ),
        ]

        report = build_report(
            rows,
            now=NOW,
            neutral_threshold_pct=0.25,
            low_sample_threshold=1,
        )

        self.assertEqual(report["evaluated_samples"], 3)
        self.assertAlmostEqual(report["overall"]["direction_accuracy"], 2 / 3, places=6)
        self.assertAlmostEqual(
            report["overall"]["mean_magnitude_error_pct_points"], 1.033333, places=4
        )
        two_hour = report["by_dimension"]["horizon"]["2h"]
        self.assertEqual(two_hour["samples"], 2)
        self.assertEqual(two_hour["confusion_matrix"]["up"]["up"], 1)
        self.assertEqual(two_hour["confusion_matrix"]["up"]["down"], 1)
        self.assertEqual(two_hour["precision_recall"]["up"]["recall"], 0.5)
        self.assertEqual(report["by_dimension"]["horizon"]["8h"]["reason"], "no_matured_samples")

    def test_reports_are_segmented_by_regime_and_low_samples_marked(self) -> None:
        report = build_report(
            [
                row(
                    origin_at="2026-09-06T00:00:00+00:00",
                    horizon=8,
                    predicted=-1.0,
                    actual=-2.0,
                    regime="high_volatility",
                    features={"volatility_24h_pct": 3.5},
                )
            ],
            now=NOW,
            low_sample_threshold=2,
        )

        self.assertIn("high_volatility", report["by_dimension"]["regime"])
        self.assertIn("high", report["by_dimension"]["volatility_bucket"])
        self.assertTrue(report["overall"]["unstable_or_low_sample"])
        self.assertIn("low_sample_count", report["overall"]["reason"])

    def test_markdown_surfaces_threshold_meaningful_moves_and_leakage_context(self) -> None:
        report = build_report([], now=NOW)
        markdown = render_markdown(report)

        self.assertIn("Directional-signal evaluation", markdown)
        self.assertIn("Neutral/meaningful-move threshold", markdown)
        self.assertIn("Direction hits are evaluated separately from magnitude error", markdown)
        self.assertTrue(report["leakage_guard"]["uses_only_matured_outcomes"])

    def test_generate_report_reads_durable_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "history.sqlite"
            store = ForecastHistoryStore(db)
            snapshot = {
                "generated_at": "2026-09-06T00:00:00+00:00",
                "latest_close_at": "2026-09-06T00:00:00+00:00",
                "latest_close_usd": 100.0,
                "source": "test",
                "pair": "BTCUSD",
                "regime": "range",
                "market_features": {"volatility_24h_pct": 0.5},
                "predictions": {"2h": {"price_usd": 102.0, "change_pct": 2.0}},
                "model_predictions": {"persistence": {"2h": {"price_usd": 100.0}}},
            }
            target = int(datetime(2026, 9, 6, 2, tzinfo=timezone.utc).timestamp())
            store.ingest_snapshot(snapshot, {target: 101.0})

            report = generate_report(
                db,
                json_path=root / "direction.json",
                markdown_path=root / "direction.md",
                neutral_threshold_pct=0.25,
                low_sample_threshold=1,
            )

            self.assertEqual(report["evaluated_samples"], 1)
            self.assertTrue((root / "direction.json").exists())
            self.assertTrue((root / "direction.md").exists())

    def test_invalid_configuration_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_report([], neutral_threshold_pct=-0.1)
        with self.assertRaises(ValueError):
            build_report([], low_sample_threshold=0)


if __name__ == "__main__":
    unittest.main()
