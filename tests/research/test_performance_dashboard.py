import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from btc_timesfm.history.history_store import ForecastHistoryStore
from btc_timesfm.research.performance_dashboard import (
    build_report,
    generate_dashboard,
    render_html,
    render_markdown,
)


NOW = datetime(2026, 9, 5, 22, 0, tzinfo=timezone.utc)


def row(
    *,
    origin_at: str,
    model: str,
    horizon: int,
    regime: str,
    mae: float,
    bias: float,
    direction: int,
    coverage: int | None,
) -> dict[str, object]:
    return {
        "origin_at": origin_at,
        "model_name": model,
        "horizon_hours": horizon,
        "regime": regime,
        "actual_target_price_usd": 100.0,
        "absolute_error_pct": mae,
        "signed_error_pct": bias,
        "direction_correct": direction,
        "within_q10_q90": coverage,
    }


class PerformanceDashboardTests(unittest.TestCase):
    def test_required_horizons_and_persistence_are_always_visible(self) -> None:
        report = build_report(
            [
                row(
                    origin_at="2026-09-05T20:00:00+00:00",
                    model="ensemble",
                    horizon=2,
                    regime="range",
                    mae=1.0,
                    bias=0.2,
                    direction=1,
                    coverage=1,
                )
            ],
            now=NOW,
            low_sample_threshold=2,
        )

        self.assertEqual(report["horizons"], ["2h", "4h", "8h", "16h"])
        for horizon in report["horizons"]:
            models = report["windows"]["all"]["horizons"][horizon]["models"]
            self.assertIn("ensemble", models)
            self.assertIn("persistence", models)

        two_hour = report["windows"]["all"]["horizons"]["2h"]
        self.assertTrue(two_hour["persistence_baseline_missing"])
        self.assertEqual(two_hour["models"]["persistence"]["confidence_warning"], "no_samples")

    def test_metrics_are_computed_per_model(self) -> None:
        rows = [
            row(
                origin_at="2026-09-05T18:00:00+00:00",
                model="ensemble",
                horizon=2,
                regime="range",
                mae=1.0,
                bias=0.5,
                direction=1,
                coverage=1,
            ),
            row(
                origin_at="2026-09-05T20:00:00+00:00",
                model="ensemble",
                horizon=2,
                regime="range",
                mae=3.0,
                bias=-0.5,
                direction=0,
                coverage=0,
            ),
            row(
                origin_at="2026-09-05T20:00:00+00:00",
                model="persistence",
                horizon=2,
                regime="range",
                mae=4.0,
                bias=1.0,
                direction=1,
                coverage=None,
            ),
        ]
        report = build_report(rows, now=NOW, low_sample_threshold=2)
        ensemble = report["windows"]["all"]["horizons"]["2h"]["models"]["ensemble"]

        self.assertEqual(ensemble["samples"], 2)
        self.assertEqual(ensemble["mae_pct"], 2.0)
        self.assertEqual(ensemble["mean_signed_error_pct"], 0.0)
        self.assertEqual(ensemble["direction_accuracy"], 0.5)
        self.assertEqual(ensemble["q10_q90_coverage"], 0.5)
        self.assertEqual(ensemble["interval_samples"], 2)
        self.assertIsNone(ensemble["confidence_warning"])

    def test_rolling_windows_and_regimes_are_separate(self) -> None:
        rows = [
            row(
                origin_at="2026-08-01T00:00:00+00:00",
                model="ensemble",
                horizon=4,
                regime="trending",
                mae=5.0,
                bias=2.0,
                direction=0,
                coverage=0,
            ),
            row(
                origin_at="2026-09-04T20:00:00+00:00",
                model="ensemble",
                horizon=4,
                regime="range",
                mae=1.0,
                bias=0.1,
                direction=1,
                coverage=1,
            ),
            row(
                origin_at="2026-09-04T20:00:00+00:00",
                model="persistence",
                horizon=4,
                regime="range",
                mae=2.0,
                bias=-0.2,
                direction=1,
                coverage=None,
            ),
        ]
        report = build_report(rows, now=NOW, rolling_days=(7, 30), low_sample_threshold=1)

        self.assertEqual(report["windows"]["7d"]["matured_rows"], 2)
        self.assertEqual(
            report["windows"]["7d"]["horizons"]["4h"]["models"]["ensemble"]["mae_pct"],
            1.0,
        )
        self.assertIn("range", report["windows"]["7d"]["horizons"]["4h"]["by_regime"])
        self.assertNotIn(
            "trending",
            report["windows"]["7d"]["horizons"]["4h"]["by_regime"],
        )

    def test_renderers_surface_warnings_and_baseline(self) -> None:
        report = build_report([], now=NOW, rolling_days=(7,), low_sample_threshold=20)
        markdown = render_markdown(report)
        html = render_html(report)

        self.assertIn("Persistence", markdown)
        self.assertIn("persistence", markdown)
        self.assertIn("no_samples", markdown)
        self.assertIn("persistence", html)
        self.assertIn("no_samples", html)

    def test_invalid_configuration_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_report([], now=NOW, low_sample_threshold=0)
        with self.assertRaises(ValueError):
            build_report([], now=NOW, rolling_days=(0,))

    def test_generate_dashboard_reads_durable_history(self) -> None:
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
                "market_features": {},
                "predictions": {
                    "2h": {
                        "price_usd": 102.0,
                        "change_pct": 2.0,
                        "q10_usd": 99.0,
                        "q50_usd": 102.0,
                        "q90_usd": 104.0,
                    }
                },
                "model_predictions": {
                    "persistence": {"2h": {"price_usd": 100.0}},
                    "timesfm_168": {"2h": {"price_usd": 103.0}},
                },
                "model_weights": {"2h": {"persistence": 0.2, "timesfm_168": 0.8}},
            }
            target = int(datetime(2026, 9, 5, 20, tzinfo=timezone.utc).timestamp())
            store.ingest_snapshot(snapshot, {target: 101.0})

            json_path = root / "dashboard.json"
            markdown_path = root / "dashboard.md"
            html_path = root / "dashboard.html"
            report = generate_dashboard(
                db,
                json_path=json_path,
                markdown_path=markdown_path,
                html_path=html_path,
                rolling_days=(7,),
                low_sample_threshold=1,
            )

            self.assertEqual(
                report["windows"]["all"]["horizons"]["2h"]["models"]["ensemble"]["samples"],
                1,
            )
            self.assertEqual(
                report["windows"]["all"]["horizons"]["2h"]["models"]["persistence"]["samples"],
                1,
            )
            self.assertTrue(json_path.exists())
            self.assertTrue(markdown_path.exists())
            self.assertTrue(html_path.exists())
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["database_verification"]["ok"])


if __name__ == "__main__":
    unittest.main()
