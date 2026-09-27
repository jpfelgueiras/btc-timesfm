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
    def test_disaster_recovery_report_is_included_and_rendered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "drill.json"
            report_path.write_text(
                json.dumps(
                    {
                        "status": "passed",
                        "checked_at": "2026-09-05T22:00:00+00:00",
                        "restore_duration_ms": 12.5,
                        "row_counts": {"forecast_origins": 2, "forecast_predictions": 4},
                    }
                )
            )
            report = build_report([], now=NOW, disaster_recovery_report_path=report_path)

        self.assertEqual(report["disaster_recovery"]["status"], "passed")
        self.assertIn("Disaster-recovery drill", render_markdown(report))

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

    def test_volatility_audit_is_descriptive_and_independent_of_future_outcomes(self) -> None:
        origin = "2026-09-05T18:00:00+00:00"
        sample = row(
            origin_at=origin,
            model="ensemble",
            horizon=2,
            regime="range",
            mae=1.0,
            bias=0.0,
            direction=1,
            coverage=1,
        )
        sample["market_features_json"] = '{"volatility_24h_pct": 1.5}'
        first = build_report([sample], now=NOW)["volatility_bucket_audit"]
        changed_outcome = dict(sample, actual_target_price_usd=999999.0, absolute_error_pct=99.0)
        second = build_report([changed_outcome], now=NOW)["volatility_bucket_audit"]

        self.assertEqual(first, second)
        self.assertEqual(first["valid_count"], 1)
        self.assertEqual(first["fixed_labels"]["counts"], {"low": 0, "medium": 1, "high": 0})
        self.assertEqual(first["conclusion"], "diagnostic_only_inconclusive")
        self.assertEqual(first["forecast_weighting"], "not permitted")

    def test_volatility_audit_reports_unavailable_feature_explicitly(self) -> None:
        missing = row(
            origin_at="2026-09-05T18:00:00+00:00",
            model="ensemble",
            horizon=2,
            regime="range",
            mae=1.0,
            bias=0.0,
            direction=1,
            coverage=1,
        )
        missing["configuration_id"] = "not-feature-provenance"
        audit = build_report([missing], now=NOW)["volatility_bucket_audit"]
        self.assertEqual(audit["status"], "unavailable")
        self.assertEqual(audit["fixed_labels"]["counts"], "unavailable")
        unknown = audit["feature_classes"]["unknown"]
        self.assertEqual(unknown["status"], "unknown")
        self.assertEqual(unknown["missing_feature_count"], 1)

    def test_volatility_audit_has_per_horizon_coverage_and_exact_pair_support(self) -> None:
        origin = "2026-09-05T18:00:00+00:00"
        ensemble = row(
            origin_at=origin,
            model="ensemble",
            horizon=2,
            regime="range",
            mae=1.0,
            bias=0.0,
            direction=1,
            coverage=1,
        )
        ensemble.update(
            market_features_json='{"volatility_24h_pct": 2.5}',
            target_at="2026-09-05T20:00:00+00:00",
        )
        persistence = dict(ensemble, model_name="persistence", absolute_error_pct=2.0)
        second_ensemble = dict(
            ensemble,
            target_at="2026-09-05T21:00:00+00:00",
            absolute_error_pct=1.5,
        )
        second_persistence = dict(
            persistence,
            target_at="2026-09-05T21:00:00+00:00",
            absolute_error_pct=2.5,
        )
        report = build_report([ensemble, persistence, second_ensemble, second_persistence], now=NOW)
        audit = report["volatility_bucket_audit"]["by_horizon"]["2h"]

        self.assertEqual(audit["origin_count"], 1)
        self.assertEqual(audit["valid_count"], 1)
        self.assertEqual(audit["missing_or_invalid_count"], 0)
        self.assertEqual(audit["fixed_labels"]["counts"], {"low": 0, "medium": 0, "high": 1})
        self.assertEqual(audit["all_origin_date_coverage"]["distinct_dates"], 1)
        self.assertEqual(audit["valid_feature_date_coverage"]["distinct_dates"], 1)
        support = audit["exact_origin_horizon_pair_support"]
        self.assertEqual(support["pair_count"], 2)
        self.assertEqual(support["by_volatility_bucket"]["high"], 2)
        self.assertFalse(support["inferential_comparison_performed"])
        markdown = render_markdown(report)
        self.assertIn("Per-horizon support", markdown)
        self.assertIn("exact paired support=", markdown)
        self.assertIn("diagnostic only", markdown)
        html = render_html(report)
        self.assertIn("All-origin date coverage:", html)
        self.assertIn("Valid-feature date coverage:", html)

    def test_future_origin_feature_mutation_does_not_change_past_bucket(self) -> None:
        earlier = row(
            origin_at="2026-09-04T18:00:00+00:00",
            model="ensemble",
            horizon=2,
            regime="range",
            mae=1.0,
            bias=0.0,
            direction=1,
            coverage=1,
        )
        later = dict(earlier, origin_at="2026-09-05T18:00:00+00:00")
        earlier["market_features_json"] = '{"volatility_24h_pct": 0.5}'
        later["market_features_json"] = '{"volatility_24h_pct": 1.5}'
        before = build_report([earlier, later], now=NOW)["volatility_bucket_audit"]["by_horizon"][
            "2h"
        ]
        later["market_features_json"] = '{"volatility_24h_pct": 99.0}'
        after = build_report([earlier, later], now=NOW)["volatility_bucket_audit"]["by_horizon"][
            "2h"
        ]

        self.assertEqual(before["fixed_labels"]["counts"]["low"], 1)
        self.assertEqual(after["fixed_labels"]["counts"]["low"], 1)
        self.assertEqual(before["fixed_labels"]["counts"]["medium"], 1)
        self.assertEqual(after["fixed_labels"]["counts"]["high"], 1)

    def test_paired_rolling_skill_scores_are_computed_against_baselines(self) -> None:
        rows = [
            row(
                origin_at="2026-08-01T00:00:00+00:00",
                model="ensemble",
                horizon=2,
                regime="range",
                mae=10.0,
                bias=0.0,
                direction=0,
                coverage=1,
            ),
            row(
                origin_at="2026-08-01T00:00:00+00:00",
                model="persistence",
                horizon=2,
                regime="range",
                mae=1.0,
                bias=0.0,
                direction=1,
                coverage=None,
            ),
            row(
                origin_at="2026-09-05T18:00:00+00:00",
                model="ensemble",
                horizon=2,
                regime="range",
                mae=1.0,
                bias=0.0,
                direction=1,
                coverage=1,
            ),
            row(
                origin_at="2026-09-05T18:00:00+00:00",
                model="persistence",
                horizon=2,
                regime="range",
                mae=2.0,
                bias=0.0,
                direction=0,
                coverage=None,
            ),
            row(
                origin_at="2026-09-05T18:00:00+00:00",
                model="timesfm_168",
                horizon=2,
                regime="range",
                mae=4.0,
                bias=0.0,
                direction=0,
                coverage=None,
            ),
        ]

        report = build_report(rows, now=NOW, rolling_days=(7,), low_sample_threshold=1)
        all_skill = report["windows"]["all"]["horizons"]["2h"]["models"]["ensemble"][
            "skill_scores"
        ]["persistence"]
        rolling_horizon = report["windows"]["7d"]["horizons"]["2h"]
        rolling_skill = rolling_horizon["models"]["ensemble"]["skill_scores"]["persistence"]
        additional_baseline_skill = rolling_horizon["models"]["ensemble"]["skill_scores"][
            "timesfm_168"
        ]

        self.assertEqual(rolling_horizon["skill_baselines"], ["persistence", "timesfm_168"])
        self.assertEqual(all_skill["paired_samples"], 2)
        self.assertEqual(all_skill["edge_state"], "negative")
        self.assertEqual(rolling_skill["paired_samples"], 1)
        self.assertEqual(rolling_skill["skill_score"], 0.5)
        self.assertEqual(rolling_skill["skill_pct"], 50.0)
        self.assertEqual(rolling_skill["edge_state"], "positive")
        self.assertEqual(additional_baseline_skill["skill_score"], 0.75)

    def test_skill_scores_are_conservative_without_samples_or_valid_baseline(self) -> None:
        rows = [
            row(
                origin_at="2026-09-05T18:00:00+00:00",
                model="ensemble",
                horizon=2,
                regime="range",
                mae=1.0,
                bias=0.0,
                direction=1,
                coverage=1,
            ),
            row(
                origin_at="2026-09-05T18:00:00+00:00",
                model="persistence",
                horizon=2,
                regime="range",
                mae=0.0,
                bias=0.0,
                direction=1,
                coverage=None,
            ),
        ]
        report = build_report(rows, now=NOW, low_sample_threshold=2)
        skill = report["windows"]["all"]["horizons"]["2h"]["models"]["ensemble"]["skill_scores"][
            "persistence"
        ]
        missing = report["windows"]["all"]["horizons"]["4h"]["models"]["ensemble"]["skill_scores"][
            "persistence"
        ]

        self.assertIsNone(skill["skill_score"])
        self.assertEqual(skill["edge_state"], "inconclusive")
        self.assertEqual(skill["confidence_warning"], "zero_baseline_error")
        self.assertEqual(missing["confidence_warning"], "no_paired_samples")

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
        self.assertIn("Skill vs persistence", markdown)
        self.assertIn("Edge", markdown)
        self.assertIn("persistence", markdown)
        self.assertIn("no_samples", markdown)
        self.assertIn("Skill vs persistence", html)
        self.assertIn("persistence", html)
        self.assertIn("no_samples", html)

    def test_invalid_configuration_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_report([], now=NOW, low_sample_threshold=0)
        with self.assertRaises(ValueError):
            build_report([], now=NOW, rolling_days=(0,))
        with self.assertRaises(ValueError):
            build_report([], now=NOW, skill_neutral_threshold=-0.1)

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

    def test_weekly_slo_adherence_section_included_when_log_provided(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_path = root / "slo_events.jsonl"
            now = datetime(2026, 9, 5, 20, 0, tzinfo=timezone.utc)
            events = [
                {
                    "timestamp": "2026-09-05T18:00:00+00:00",
                    "event": "check_ok",
                    "metric_name": "production_forecast",
                },
                {
                    "timestamp": "2026-09-05T18:30:00+00:00",
                    "event": "breach",
                    "metric_name": "production_forecast",
                },
                {
                    "timestamp": "2026-09-05T19:00:00+00:00",
                    "event": "check_ok",
                    "metric_name": "site_update",
                },
            ]
            log_path.write_text(
                "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n",
                encoding="utf-8",
            )

            report = build_report([], now=now, slo_metrics_log_path=log_path)
            slo = report["freshness_slo"]
            self.assertEqual(slo["overall"]["checks"], 3)
            self.assertEqual(slo["overall"]["breaches"], 1)
            self.assertEqual(slo["per_metric"]["production_forecast"]["adherence_pct"], 50.0)
            markdown = render_markdown(report)
            self.assertIn("Weekly freshness SLO adherence", markdown)
            html = render_html(report)
            self.assertIn("Weekly freshness SLO adherence", html)

    def test_report_without_slo_log_has_no_slo_section(self) -> None:
        report = build_report([], now=NOW)
        self.assertNotIn("freshness_slo", report)


if __name__ == "__main__":
    unittest.main()
