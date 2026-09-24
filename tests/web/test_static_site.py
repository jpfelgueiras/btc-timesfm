from __future__ import annotations

import unittest
from datetime import datetime, timezone

from btc_timesfm.web.historical_explorer import build_explorer_data, render_explorer
from btc_timesfm.web.static_site import (
    _render_latest,
    _render_accuracy,
    _render_explorer,
    _render_recent,
    _utc_label,
    build_site_data,
    explorer_query,
    explorer_url_state,
    filter_explorer_rows,
    render_html,
)


class StaticSiteTests(unittest.TestCase):
    def test_uncertainty_and_performance_copy_is_qualified(self) -> None:
        latest_html = _render_latest(
            {
                "latest": {
                    "source_price_usd": 64000,
                    "origin_at": "2026-09-07T12:00:00Z",
                    "predictions": [
                        {
                            "horizon_hours": 4,
                            "target_at": "2026-09-07T16:00:00Z",
                            "predicted_price_usd": 64640,
                            "predicted_change_pct": 1.0,
                            "q10_usd": 63000,
                            "q90_usd": 66000,
                        }
                    ],
                },
                "latest_age_hours": 2.0,
            }
        )
        self.assertIn("q10–q90 prediction interval", latest_html)
        self.assertIn("not a probability of gain", latest_html)
        self.assertNotIn("confidence score", latest_html.lower())

        absent = _render_latest(
            {
                "latest": {
                    "source_price_usd": 64000,
                    "origin_at": "2026-09-07T12:00:00Z",
                    "predictions": [
                        {
                            "horizon_hours": 4,
                            "target_at": "2026-09-07T16:00:00Z",
                            "predicted_price_usd": 64640,
                            "predicted_change_pct": 1.0,
                            "q10_usd": None,
                            "q90_usd": None,
                        }
                    ],
                },
                "latest_age_hours": 2.0,
            }
        )
        self.assertNotIn("prediction interval", absent)
        self.assertNotIn("P(up)", absent)

    def test_accuracy_copy_exposes_window_horizon_n_and_low_sample(self) -> None:
        rendered = _render_accuracy(
            {
                "accuracy": {
                    "7d": {
                        "4h": {
                            "samples": 2,
                            "mae_pct": 1.2,
                            "direction_accuracy": 0.5,
                            "q10_q90_coverage": 0.5,
                            "confidence_warning": "low_sample",
                        }
                    }
                },
                "horizons": ["4h"],
            }
        )
        self.assertIn("7 days", rendered)
        self.assertIn("4h", rendered)
        self.assertIn("Sample count (n)", rendered)
        self.assertIn('2 <span class="sub">Low sample</span>', rendered)
        self.assertIn("q10–q90 coverage", rendered)
        self.assertIn('href="?days=7&amp;horizon=4#explorer"', rendered)
        self.assertIn("Inspect 7 days 4h forecast records", rendered)

    def test_latest_summary_names_values_and_distinguishes_unknown_age(self) -> None:
        latest = {
            "source_price_usd": 64000,
            "origin_at": "2026-09-07T12:00:00+00:00",
            "regime": "range",
            "predictions": [
                {
                    "horizon_hours": 4,
                    "target_at": "2026-09-07T16:00:00+00:00",
                    "predicted_price_usd": 64640,
                    "predicted_change_pct": 1.0,
                    "q10_usd": 63000,
                    "q90_usd": 66000,
                }
            ],
        }
        rendered = _render_latest(
            {"latest": latest, "latest_age_hours": None, "generated_at": "2026-09-07T13:00:00Z"}
        )

        self.assertIn("Observed BTC source price", rendered)
        self.assertIn("$64,000", rendered)
        self.assertIn("Predicted BTC price", rendered)
        self.assertIn("$64,640", rendered)
        self.assertIn("Up · +1.00%", rendered)
        self.assertIn("+4h horizon", rendered)
        self.assertIn("2026-09-07 16:00 UTC", rendered)
        self.assertIn("Forecast age unknown", rendered)
        self.assertNotIn("LIVE", rendered)

    def test_latest_summary_fresh_stale_and_missing_states(self) -> None:
        base = {
            "latest": {
                "source_price_usd": 64000,
                "origin_at": "2026-09-07T12:00:00Z",
                "predictions": [],
            },
            "generated_at": "2026-09-07T12:00:00Z",
        }
        self.assertIn("Forecast recent", _render_latest({**base, "latest_age_hours": 4.0}))
        self.assertIn("Forecast stale", _render_latest({**base, "latest_age_hours": 4.01}))
        missing = _render_latest({"latest": None, "explorer": {"rows": []}})
        self.assertIn("No forecast history is available", missing)
        self.assertEqual(_utc_label(None), "Unknown (UTC)")
        self.assertEqual(_utc_label("2026-09-07T12:00:00Z"), "2026-09-07 12:00 UTC")

    def test_empty_dashboard_states_are_distinct_and_recoverable(self) -> None:
        no_history = _render_latest({"latest": None, "explorer": {"rows": []}})
        latest_missing = _render_latest(
            {"latest": None, "explorer": {"rows": [{"origin_at": "old"}]}}
        )
        explorer_empty = _render_explorer({"explorer": {"rows": [], "horizons": []}})
        explorer_with_history = _render_explorer(
            {
                "explorer": {
                    "horizons": [2],
                    "rows": [
                        {
                            "origin_at": "2026-09-07T12:00:00Z",
                            "horizon_hours": 2,
                            "target_at": "2026-09-07T14:00:00Z",
                            "status": "pending",
                            "regime": "range",
                            "source_price_usd": 64000,
                            "predicted_price_usd": 64500,
                            "predicted_change_pct": 0.78,
                            "q10_usd": 63000,
                            "q90_usd": 66000,
                            "actual_target_price_usd": None,
                            "actual_change_pct": None,
                            "absolute_error_pct": None,
                            "direction_correct": None,
                        }
                    ],
                }
            }
        )

        self.assertIn("No forecast history is available yet", no_history)
        self.assertIn("Forecast history exists, but no latest prediction", latest_missing)
        self.assertIn("No forecast records are available in this static snapshot", explorer_empty)
        self.assertIn("No forecasts match these filters", explorer_with_history)
        self.assertIn("No recent forecast rows are available", _render_recent({"recent": []}))

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
            self.assertEqual(window["by_volatility_bucket"]["high"]["mae_delta_pct_points"], 2.0)

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

    def test_explorer_filter_logic_and_url_state(self) -> None:
        rows = [
            {
                "origin_at": "2026-09-07T12:00:00+00:00",
                "horizon_hours": 2,
                "status": "pending",
                "regime": "range",
                "absolute_error_pct": None,
            },
            {
                "origin_at": "2026-09-01T12:00:00+00:00",
                "horizon_hours": 4,
                "status": "matured",
                "regime": "trending",
                "absolute_error_pct": 1.0,
            },
        ]
        state = explorer_url_state("?days=7&horizon=2&q=range&sort=error", [2, 4])
        selected = filter_explorer_rows(
            rows, state, now=datetime(2026, 9, 7, 13, tzinfo=timezone.utc)
        )

        self.assertEqual(selected, rows[:1])
        self.assertEqual(explorer_query(state), "days=7&horizon=2&q=range&sort=error")
        self.assertEqual(explorer_url_state("?days=x&horizon=3&origin=bad", [2, 4])["days"], "all")
        self.assertIsNone(explorer_url_state("?days=x&horizon=3&origin=bad", [2, 4])["origin"])

    def test_historical_explorer_preserves_forecasts_and_gates_unmatured_outcomes(self) -> None:
        rows = [
            self._row(
                origin="2026-09-07T10:00:00+00:00",
                horizon=2,
                predicted=101.25,
                change=1.25,
                actual=100.0,
                error=1.25,
                direction=0,
            ),
            self._row(
                origin="2026-09-07T12:00:00+00:00",
                horizon=4,
                predicted=103.5,
                change=3.5,
                actual=99.0,
                error=4.5,
                direction=0,
            ),
        ]
        rows[0]["configuration_id"] = "champion-cfg"
        rows[1]["experiment_manifest_json"] = "not json"
        data = build_explorer_data(
            rows,
            now=datetime(2026, 9, 7, 13, tzinfo=timezone.utc),
            configuration_roles={"champion-cfg": "champion"},
        )

        self.assertEqual(data["dates"], ["2026-09-07"])
        self.assertEqual(data["horizons"], [2, 4])
        matured, pending = sorted(data["forecasts"], key=lambda item: item["horizon_hours"])
        self.assertEqual(matured["predicted_price_usd"], 101.25)
        self.assertEqual(matured["identity"]["role"], "champion")
        self.assertEqual(pending["maturity"], "pending")
        self.assertIsNone(pending["actual_target_price_usd"])
        self.assertIsNone(pending["absolute_error_pct"])
        self.assertEqual(pending["identity"]["configuration_id"], "metadata unavailable")
        page = render_explorer(data)
        self.assertIn("Forecast History Explorer", page)
        self.assertIn('id="history-days"', page)
        self.assertIn('id="history-horizon"', page)
        self.assertIn('id="history-status"', page)
        self.assertIn('id="history-model"', page)
        self.assertIn("history.pushState", page)
        self.assertIn("history-no-matches", page)
        self.assertIn("Pending maturity", page)
        self.assertNotIn("$99.00 / 4.50%", page)
        self.assertIn("champion", page)

    def test_large_history_remains_bounded_and_filterable(self) -> None:
        rows = [
            self._row(
                origin=f"2026-09-{(index % 28) + 1:02d}T{index % 24:02d}:00:00+00:00",
                horizon=(2, 4, 8, 16)[index % 4],
                predicted=101.0 + index,
                change=1.0,
                actual=101.5 if index % 2 else None,
                error=0.49 if index % 2 else None,
                direction=1 if index % 2 else None,
                model="ensemble" if index % 3 else "persistence",
            )
            for index in range(600)
        ]
        history = build_explorer_data(rows, now=datetime(2026, 9, 29, tzinfo=timezone.utc))
        page = render_explorer(history)

        self.assertEqual(len(history["forecasts"]), 600)
        self.assertEqual(page.count('data-origin="'), 600)
        self.assertLess(len(page.encode("utf-8")), 5 * 1024 * 1024)
        self.assertIn("All available history", page)
        self.assertIn("Matured", page)
        self.assertIn("Pending", page)
        self.assertIn("All models", page)

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
        self.assertIn('role="tablist"', page)
        self.assertIn("Overview", page)
        self.assertIn("Model Metrics", page)
        self.assertIn('role="tabpanel"', page)
        self.assertIn("ArrowRight", page)
        self.assertIn("$102", page)
        self.assertIn("Ensemble edge vs persistence", page)
        self.assertIn("Paired samples", page)
        self.assertIn("7 days", page)
        self.assertIn("30 days", page)
        self.assertIn("90 days", page)
        self.assertIn("All time", page)
        self.assertNotIn("Recent forecast ledger", page)
        self.assertNotIn('class="table-wrap recent-table"', page)
        self.assertNotIn('id="explorer-table"', page)
        self.assertIn("Forecast History Explorer", page)
        self.assertIn('id="history-horizon"', page)
        self.assertIn("Uncertainty interval", page)
        self.assertIn("history-detail", page)
        self.assertEqual(page.count('id="explorer"'), 1)
        self.assertIn("history.replaceState", page)
        self.assertIn("location.search", page)
        self.assertIn("No X/Twitter dependency", page)
        self.assertIn("Pending", page)
        self.assertIn("row.dataset.model", page)


if __name__ == "__main__":
    unittest.main()
