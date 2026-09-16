from __future__ import annotations

import unittest
from datetime import datetime, timezone

from btc_timesfm.web.static_site import build_site_data, render_html


class StaticSiteTests(unittest.TestCase):
    def _row(
        self,
        *,
        origin: str,
        horizon: int,
        predicted: float,
        change: float,
        actual: float | None,
        error: float | None,
        direction: int | None,
        model: str = "ensemble",
        regime: str = "range",
        volatility: float = 0.5,
    ) -> dict[str, object]:
        return {
            "generated_at": origin,
            "origin_at": origin,
            "source_name": "kraken",
            "pair": "BTC/USD",
            "source_price_usd": 100.0,
            "regime": regime,
            "market_features_json": f'{{"volatility_24h_pct": {volatility}}}',
            "experiment_manifest_json": '{"configuration": {"feature_set_version": "test"}}',
            "model_name": model,
            "horizon_hours": horizon,
            "target_at": f"2026-09-07T{12 + horizon:02d}:00:00+00:00",
            "predicted_price_usd": predicted,
            "predicted_change_pct": change,
            "q10_usd": predicted - 2.0,
            "q50_usd": predicted,
            "q90_usd": predicted + 2.0,
            "actual_target_price_usd": actual,
            "absolute_error_pct": error,
            "signed_error_pct": error,
            "actual_change_pct": 1.0 if actual is not None else None,
            "direction_correct": direction,
            "within_q10_q90": 1 if actual is not None else None,
        }

    def test_build_site_data_uses_latest_origin_and_accuracy(self) -> None:
        rows = [
            self._row(
                origin="2026-09-07T10:00:00+00:00",
                horizon=2,
                predicted=101.0,
                change=1.0,
                actual=101.5,
                error=0.49,
                direction=1,
            ),
            self._row(
                origin="2026-09-07T12:00:00+00:00",
                horizon=2,
                predicted=102.0,
                change=2.0,
                actual=None,
                error=None,
                direction=None,
            ),
            self._row(
                origin="2026-09-07T12:00:00+00:00",
                horizon=4,
                predicted=99.0,
                change=-1.0,
                actual=None,
                error=None,
                direction=None,
            ),
        ]
        data = build_site_data(
            rows,
            now=datetime(2026, 9, 7, 13, tzinfo=timezone.utc),
            recent_limit=10,
        )

        self.assertEqual(data["latest"]["origin_at"], "2026-09-07T12:00:00+00:00")
        self.assertEqual(len(data["latest"]["predictions"]), 2)
        self.assertEqual(data["latest_age_hours"], 1.0)
        self.assertEqual(data["accuracy"]["all"]["2h"]["samples"], 1)
        self.assertAlmostEqual(data["accuracy"]["all"]["2h"]["direction_accuracy"], 1.0)

    def test_build_site_data_exposes_latest_coherence_diagnostics(self) -> None:
        snapshot = {
            "multi_horizon_coherence": {
                "coherence_score": 1.0,
                "violation_log": {"crossing_entries": 0},
            }
        }
        data = build_site_data(
            [
                self._row(
                    origin="2026-09-07T12:00:00+00:00",
                    horizon=2,
                    predicted=102.0,
                    change=2.0,
                    actual=None,
                    error=None,
                    direction=None,
                )
            ],
            now=datetime(2026, 9, 7, 13, tzinfo=timezone.utc),
            latest_snapshot=snapshot,
        )

        self.assertEqual(data["multi_horizon_coherence"], snapshot["multi_horizon_coherence"])

    def test_persistence_edge_matches_paired_report_numbers(self) -> None:
        rows = [
            self._row(
                origin="2026-09-07T10:00:00+00:00",
                horizon=2,
                predicted=101.0,
                change=1.0,
                actual=101.0,
                error=1.0,
                direction=1,
                model="ensemble",
                regime="trending",
                volatility=3.0,
            ),
            self._row(
                origin="2026-09-07T10:00:00+00:00",
                horizon=2,
                predicted=100.0,
                change=0.0,
                actual=101.0,
                error=3.0,
                direction=0,
                model="persistence",
                regime="trending",
                volatility=3.0,
            ),
        ]
        data = build_site_data(rows, now=datetime(2026, 9, 7, 13, tzinfo=timezone.utc))

        windows = data["persistence_edge"]["windows"]
        self.assertEqual(tuple(windows), ("7d", "30d", "90d", "all"))
        for window in windows.values():
            horizon = window["by_horizon"]["2h"]
            self.assertEqual(horizon["samples"], 1)
            self.assertEqual(horizon["mae_delta_pct_points"], 2.0)
            self.assertTrue(horizon["unstable_or_low_sample"])
            self.assertEqual(window["by_regime"]["trending"]["mae_delta_pct_points"], 2.0)
            self.assertEqual(
                window["by_volatility_bucket"]["high"]["mae_delta_pct_points"], 2.0
            )

    def test_persistence_edge_windows_exclude_old_pairs(self) -> None:
        rows = []
        for origin, ensemble_error, persistence_error in (
            ("2026-09-07T10:00:00+00:00", 1.0, 3.0),
            ("2026-08-20T10:00:00+00:00", 4.0, 1.0),
            ("2026-07-01T10:00:00+00:00", 4.0, 1.0),
            ("2026-05-01T10:00:00+00:00", 4.0, 1.0),
        ):
            rows.extend(
                [
                    self._row(
                        origin=origin,
                        horizon=2,
                        predicted=101.0,
                        change=1.0,
                        actual=101.0,
                        error=ensemble_error,
                        direction=1,
                    ),
                    self._row(
                        origin=origin,
                        horizon=2,
                        predicted=100.0,
                        change=0.0,
                        actual=101.0,
                        error=persistence_error,
                        direction=0,
                        model="persistence",
                    ),
                ]
            )
        data = build_site_data(rows, now=datetime(2026, 9, 7, 13, tzinfo=timezone.utc))
        windows = data["persistence_edge"]["windows"]

        self.assertEqual(windows["7d"]["by_horizon"]["2h"]["samples"], 1)
        self.assertEqual(windows["30d"]["by_horizon"]["2h"]["samples"], 2)
        self.assertEqual(windows["90d"]["by_horizon"]["2h"]["samples"], 3)
        self.assertEqual(windows["all"]["by_horizon"]["2h"]["samples"], 4)
        self.assertEqual(windows["7d"]["by_horizon"]["2h"]["mae_delta_pct_points"], 2.0)
        self.assertEqual(windows["all"]["by_horizon"]["2h"]["mae_delta_pct_points"], -1.75)

    def test_render_html_contains_predictions_accuracy_and_ledger(self) -> None:
        rows = [
            self._row(
                origin="2026-09-07T12:00:00+00:00",
                horizon=2,
                predicted=102.0,
                change=2.0,
                actual=None,
                error=None,
                direction=None,
            )
        ]
        data = build_site_data(
            rows,
            now=datetime(2026, 9, 7, 13, tzinfo=timezone.utc),
        )
        page = render_html(data)

        self.assertIn("Forecasts & accuracy", page)
        self.assertIn("$102", page)
        self.assertIn("Ensemble edge vs persistence", page)
        self.assertIn("Paired samples", page)
        self.assertIn("7 days", page)
        self.assertIn("30 days", page)
        self.assertIn("90 days", page)
        self.assertIn("All time", page)
        self.assertIn("Recent forecast ledger", page)
        self.assertIn("No X/Twitter dependency", page)
        self.assertIn("Pending", page)


if __name__ == "__main__":
    unittest.main()
