import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from btc_timesfm.history.history_store import ForecastHistoryStore
from btc_timesfm.research.event_aware_evaluation import (
    build_report,
    classify_direction,
    generate_report,
)
from btc_timesfm.research.event_calendar import CATEGORY_FOMC, EventCalendar

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
FOMC_2026_09 = "2026-09-16T18:00:00+00:00"


def iso(hour: int, day: int = 16, prefix: str = "2026-09") -> str:
    return f"{prefix}-{day:02d}T{hour:02d}:00:00+00:00"


def row(
    origin_at: str,
    predicted: float,
    actual: float,
    *,
    horizon: int = 2,
    model: str = "ensemble",
    target: str | None = None,
    q10: float | None = None,
    q50: float | None = None,
    q90: float | None = None,
    within: float | None = None,
    actual_price: float | None = 100.0,
) -> dict[str, object]:
    return {
        "origin_at": origin_at,
        "target_at": target or origin_at,
        "model_name": model,
        "horizon_hours": horizon,
        "predicted_change_pct": predicted,
        "actual_change_pct": actual,
        "absolute_error_pct": abs(predicted - actual),
        "signed_error_pct": predicted - actual,
        "q10_usd": q10,
        "q50_usd": q50,
        "q90_usd": q90,
        "within_q10_q90": within,
        "actual_target_price_usd": actual_price,
    }


def fomc_calendar(timestamp: str = FOMC_2026_09, *, known_scheduled: bool = True) -> EventCalendar:
    return EventCalendar(
        [
            {
                "event_id": "fomc-test",
                "name": "FOMC statement",
                "category": CATEGORY_FOMC,
                "timestamp": timestamp,
                "known_scheduled": known_scheduled,
                "source": "test-source-v1",
            }
        ]
    )


class EventAwareEvaluationTests(unittest.TestCase):
    def test_classify_direction_uses_configurable_neutral_threshold(self) -> None:
        self.assertEqual(classify_direction(0.3, 0.25), "up")
        self.assertEqual(classify_direction(-0.3, 0.25), "down")
        self.assertEqual(classify_direction(0.1, 0.25), "neutral")
        with self.assertRaises(ValueError):
            classify_direction(0.0, -0.1)

    def test_segments_forecasts_into_pre_post_and_non_event(self) -> None:
        rows = [
            row(iso(6), 1.0, 1.0),
            row(iso(9), -1.0, 1.0),
            row(iso(12), 1.0, 2.0),
            row(iso(20), 1.0, -1.0),
            row(iso(0, day=10), 0.1, 0.2),
        ]
        report = build_report(
            rows,
            fomc_calendar(),
            now=NOW,
            window_hours=12,
            low_sample_threshold=1,
        )
        self.assertEqual(report["segments"]["pre_event"]["samples"], 3)
        self.assertEqual(report["segments"]["post_event"]["samples"], 1)
        self.assertEqual(report["segments"]["non_event"]["samples"], 1)
        self.assertEqual(report["matured_rows"], 5)
        self.assertEqual(report["evaluated_rows"], 5)

    def test_nearest_event_governs_overlapping_windows(self) -> None:
        calendar = EventCalendar(
            [
                {
                    "event_id": "e1",
                    "name": "First release",
                    "category": "data",
                    "timestamp": iso(0, day=10),
                    "known_scheduled": True,
                    "source": "test",
                },
                {
                    "event_id": "e2",
                    "name": "Second release",
                    "category": "data",
                    "timestamp": iso(12, day=10),
                    "known_scheduled": True,
                    "source": "test",
                },
            ]
        )
        rows = [
            row(iso(4, day=10), 1.0, 1.0),
            row(iso(10, day=10), 1.0, 1.0),
        ]
        report = build_report(
            rows,
            calendar,
            now=NOW,
            window_hours=24,
            low_sample_threshold=1,
        )
        self.assertEqual(report["segments"]["pre_event"]["samples"], 1)
        self.assertEqual(report["segments"]["post_event"]["samples"], 1)
        self.assertEqual(report["segments"]["non_event"]["samples"], 0)
        self.assertEqual(report["by_category"]["pre_event"]["counts"], {"data": 1})
        self.assertEqual(report["by_category"]["post_event"]["counts"], {"data": 1})

    def test_metrics_are_computed_per_segment(self) -> None:
        rows = [
            row(iso(6), 1.0, 1.0, q10=90.0, q50=100.0, q90=110.0),
            row(iso(9), -2.0, 1.0, q10=105.0, q50=102.0, q90=110.0),
            row(iso(20), 1.0, 1.0, q10=95.0, q50=100.0, q90=105.0),
        ]
        report = build_report(
            rows,
            fomc_calendar(),
            now=NOW,
            window_hours=12,
            low_sample_threshold=1,
        )
        pre = report["segments"]["pre_event"]
        self.assertEqual(pre["samples"], 2)
        self.assertAlmostEqual(pre["mae_pct_points"], 1.5)
        self.assertAlmostEqual(pre["direction_accuracy"], 0.5)
        self.assertAlmostEqual(pre["q10_q90_coverage"], 0.5)
        self.assertAlmostEqual(pre["mean_signed_error_pct_points"], -1.5)
        post = report["segments"]["post_event"]
        self.assertEqual(post["samples"], 1)
        self.assertAlmostEqual(post["q50_mae_pct"], 0.0)
        self.assertAlmostEqual(post["q50_bias_pct"], 0.0)

    def test_coverage_prefers_within_field_and_q50_stats_from_quantiles(self) -> None:
        rows = [
            row(iso(5), 1.0, 1.0, within=1.0, q10=150.0, q90=190.0, q50=110.0),
            row(iso(6), 1.0, 1.0, q10=90.0, q90=110.0, q50=110.0),
        ]
        report = build_report(
            rows,
            fomc_calendar(),
            now=NOW,
            low_sample_threshold=1,
        )
        pre = report["segments"]["pre_event"]
        self.assertEqual(pre["coverage_samples"], 2)
        self.assertAlmostEqual(pre["q10_q90_coverage"], 1.0)
        self.assertAlmostEqual(pre["q50_mae_pct"], 10.0)
        self.assertAlmostEqual(pre["q50_bias_pct"], 10.0)

    def test_leakage_guard_excludes_unresolved_and_never_reads_event_outcomes(self) -> None:
        rows = [
            row(iso(6), 1.0, 1.0, target=iso(23, day=17)),
            row(iso(9), 1.0, 1.0, target=iso(19, day=16)),
        ]
        report = build_report(
            rows,
            fomc_calendar(),
            now=NOW,
            low_sample_threshold=1,
        )
        self.assertEqual(report["excluded"]["target_not_resolved_by_now"], 1)
        self.assertEqual(report["matured_rows"], 1)
        guard = report["leakage_guard"]
        self.assertFalse(guard["uses_event_outcomes"])
        self.assertFalse(guard["uses_event_content"])
        self.assertTrue(guard["uses_only_matured_outcomes"])
        self.assertEqual(guard["segmentation_inputs"], ["event_timestamp", "origin_at"])
        self.assertIn("announced schedule timestamp", guard["pre_event_rule"])

    def test_known_scheduled_only_ignores_unscheduled_events_by_default(self) -> None:
        rows = [row(iso(6), 1.0, 1.0)]
        calendar = fomc_calendar(known_scheduled=False)
        default = build_report(
            rows,
            calendar,
            now=NOW,
            low_sample_threshold=1,
        )
        self.assertEqual(default["segments"]["non_event"]["samples"], 1)
        self.assertEqual(default["segments"]["pre_event"]["samples"], 0)
        inclusive = build_report(
            rows,
            calendar,
            now=NOW,
            known_scheduled_only=False,
            low_sample_threshold=1,
        )
        self.assertEqual(inclusive["segments"]["pre_event"]["samples"], 1)
        self.assertEqual(inclusive["segments"]["non_event"]["samples"], 0)

    def test_sample_size_limits_are_explicit(self) -> None:
        rows = [row(iso(6), 1.0, 1.0)]
        report = build_report(
            rows,
            fomc_calendar(),
            now=NOW,
            low_sample_threshold=5,
            min_compare_samples=3,
        )
        self.assertTrue(report["segments"]["pre_event"]["unstable_or_low_sample"])
        self.assertIn("low_sample_count", report["segments"]["pre_event"]["reason"])
        self.assertTrue(report["segments"]["non_event"]["unstable_or_low_sample"])
        self.assertEqual(report["segments"]["non_event"]["reason"], "no_samples")
        comparison = report["comparisons"]["mae_pct_points"]["pre_event_vs_non_event"]
        self.assertEqual(comparison["reason"], "insufficient_samples")
        self.assertEqual(comparison["conclusion"], "inconclusive")
        self.assertEqual(comparison["samples"], {"segment": 1, "baseline": 0})

    def test_empty_input_reports_explicit_zero_samples(self) -> None:
        report = build_report([], fomc_calendar(), now=NOW)
        self.assertEqual(report["matured_rows"], 0)
        self.assertEqual(report["evaluated_rows"], 0)
        for segment in ("pre_event", "post_event", "non_event"):
            self.assertEqual(report["segments"][segment]["samples"], 0)
            self.assertTrue(report["segments"][segment]["unstable_or_low_sample"])
            self.assertIsNone(report["segments"][segment]["mae_pct_points"])

    def test_comparisons_expose_bootstrap_between_segments(self) -> None:
        rows = [row(iso(6), 1.0, 1.0) for _ in range(10)] + [
            row(iso(0, day=1), 0.1, 0.2) for _ in range(10)
        ]
        report = build_report(
            rows,
            fomc_calendar(),
            now=NOW,
            window_hours=24,
            min_compare_samples=5,
            bootstrap_iterations=200,
        )
        expected_samples = {
            "mae_pct_points": {"segment": 10, "baseline": 10},
            "direction_accuracy": {"segment": 10, "baseline": 10},
            "q10_q90_coverage": {"segment": 0, "baseline": 0},
        }
        for metric in ("mae_pct_points", "direction_accuracy", "q10_q90_coverage"):
            comparison = report["comparisons"][metric]["pre_event_vs_non_event"]
            self.assertEqual(comparison["samples"], expected_samples[metric])
            self.assertIn(
                comparison["conclusion"], ("segment_better", "non_event_better", "inconclusive")
            )
            if comparison["probability_segment_better"] is not None:
                self.assertGreaterEqual(comparison["probability_segment_better"], 0.0)
                self.assertLessEqual(comparison["probability_segment_better"], 1.0)

    def test_reports_are_deterministic_and_json_serializable(self) -> None:
        rows = [
            row(iso(6), 1.0, 1.0),
            row(iso(9), -1.0, 1.0),
            row(iso(20), 1.0, -1.0),
            row(iso(0, day=10), 0.1, 0.2),
        ]
        first = build_report(rows, fomc_calendar(), now=NOW, low_sample_threshold=1)
        second = build_report(rows, fomc_calendar(), now=NOW, low_sample_threshold=1)
        self.assertEqual(first, second)
        json.dumps(first)

    def test_model_filter_excludes_other_models(self) -> None:
        rows = [
            row(iso(6), 1.0, 1.0),
            row(iso(6), 1.0, 1.0, model="persistence"),
        ]
        report = build_report(
            rows,
            fomc_calendar(),
            now=NOW,
            low_sample_threshold=1,
        )
        self.assertEqual(report["evaluated_rows"], 1)
        self.assertEqual(report["excluded"]["unsupported_model"], 1)

    def test_by_horizon_breakdown_is_reported(self) -> None:
        rows = [
            row(iso(6), 1.0, 1.0, horizon=2),
            row(iso(9), 1.0, 1.0, horizon=4),
        ]
        report = build_report(rows, fomc_calendar(), now=NOW, low_sample_threshold=1)
        self.assertEqual(report["by_horizon"]["pre_event"]["2h"]["samples"], 1)
        self.assertEqual(report["by_horizon"]["pre_event"]["4h"]["samples"], 1)

    def test_invalid_configuration_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_report([], now=NOW, window_hours=0)
        with self.assertRaises(ValueError):
            build_report([], now=NOW, neutral_threshold_pct=-0.1)
        with self.assertRaises(ValueError):
            build_report([], now=NOW, low_sample_threshold=0)
        with self.assertRaises(ValueError):
            build_report([], now=NOW, bootstrap_iterations=99)
        with self.assertRaises(ValueError):
            build_report([], now=NOW, min_compare_samples=0)

    def test_generate_report_reads_durable_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "history.sqlite"
            store = ForecastHistoryStore(db)
            snapshot = {
                "generated_at": "2026-08-01T00:00:00+00:00",
                "latest_close_at": "2026-08-01T00:00:00+00:00",
                "latest_close_usd": 100.0,
                "source": "test",
                "pair": "BTCUSD",
                "regime": "range",
                "market_features": {"volatility_24h_pct": 0.5},
                "predictions": {"16h": {"price_usd": 102.0, "change_pct": 2.0}},
                "model_predictions": {"persistence": {"16h": {"price_usd": 100.0}}},
            }
            target = int(datetime(2026, 8, 1, 16, tzinfo=timezone.utc).timestamp())
            store.ingest_snapshot(snapshot, {target: 101.0})

            report = generate_report(
                db,
                json_path=root / "event_eval.json",
                calendar=fomc_calendar(timestamp="2026-08-01T02:00:00+00:00"),
                low_sample_threshold=1,
                now=NOW,
            )
            self.assertEqual(report["matured_rows"], 1)
            self.assertEqual(report["segments"]["pre_event"]["samples"], 1)
            self.assertTrue((root / "event_eval.json").exists())


if __name__ == "__main__":
    unittest.main()
