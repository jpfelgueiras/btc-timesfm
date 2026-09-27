from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from btc_timesfm.web.charts import MAX_CHART_PAYLOAD_BYTES, MAX_POINTS_PER_CHART, render_charts


class ChartTests(unittest.TestCase):
    def _row(self, **changes: object) -> dict[str, object]:
        row: dict[str, object] = {
            "model_name": "ensemble",
            "origin_at": "2026-09-07T10:00:00+00:00",
            "horizon_hours": 2,
            "q10_usd": 98.0,
            "q50_usd": 100.0,
            "q90_usd": 102.0,
            "actual_target_price_usd": 101.0,
            "absolute_error_pct": 1.0,
            "direction_correct": 1,
            "within_q10_q90": 1,
            "regime": "trending",
        }
        row.update(changes)
        return row

    def test_renders_fan_and_performance_with_semantics(self) -> None:
        html, summary = render_charts([self._row()], ["2h"], 5)

        self.assertIn("quantile fan chart", html)
        self.assertIn("rolling performance", html)
        self.assertIn('class="chart-actual"', html)
        self.assertIn("Realized actual; regime trending", html)
        self.assertIn("Chart data summary", html)
        self.assertIn("chart-low-shading", html)
        self.assertIn('<svg class="chart" width="760" height="270"', html)
        self.assertIn('preserveAspectRatio="xMidYMid meet"', html)
        self.assertNotIn('height="auto"', html)
        self.assertIn('points="388.00,224.00 388.00,28.00"', html)
        self.assertIn("Each point is a matured forecast issued at the labeled origin date", html)
        self.assertIn("2026-09-07T10:00:00+00:00 · 2h", html)
        self.assertIn("Exact sampled forecast points", html)
        self.assertIn("q10 to q90 prediction interval", html)
        self.assertIn("USD price", html)
        self.assertEqual(summary["2h"]["matured_samples"], 1)

    def test_degenerate_and_missing_values_are_bounded_and_have_fallback(self) -> None:
        html, _ = render_charts(
            [
                self._row(
                    q10_usd=100.0, q50_usd=100.0, q90_usd=100.0, actual_target_price_usd=100.0
                ),
                self._row(q10_usd=None),
            ],
            ["2h", "4h"],
            5,
        )

        self.assertLess(len(html), MAX_CHART_PAYLOAD_BYTES)
        self.assertNotIn("nan", html.lower())
        self.assertNotIn("inf", html.lower())
        self.assertIn("No matured forecasts with q10", html)
        self.assertIn("Check back after outcomes mature", html)

    def test_empty_performance_explains_pending_outcomes(self) -> None:
        html, _ = render_charts([], ["2h"], 5)

        self.assertIn("No matured outcomes are available for this horizon yet", html)
        self.assertIn("Check back after target candles arrive", html)

    def test_escapes_untrusted_regime_text(self) -> None:
        html, _ = render_charts([self._row(regime="<script>alert(1)</script>")], ["2h"], 1)

        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_rolling_metrics_use_full_history_before_render_sampling(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = []
        for index in range(101):
            origin = start + timedelta(hours=index * (3 if index % 7 == 0 else 1))
            rows.append(
                self._row(
                    origin_at=origin.isoformat(),
                    absolute_error_pct=float(index),
                    direction_correct=index % 2,
                    within_q10_q90=int(index % 3 == 0),
                )
            )
        html, summary = render_charts(rows, ["2h"], 20)

        # The 20-origin series includes its partial-window warmup and final complete window.
        self.assertIn("MAE % (%) · 0.00–86.20", html)
        self.assertIn("current n=20", html)
        self.assertIn("span=", html)
        self.assertIn("20-origin rolling window", html)
        self.assertIn(f"<td>{MAX_POINTS_PER_CHART}</td>", html)
        self.assertEqual(summary["2h"]["matured_samples"], 101)

    def test_missing_values_and_persistence_pairing_remain_metric_specific(self) -> None:
        rows = []
        for index in range(25):
            origin = (
                datetime(2026, 9, 1, 10, tzinfo=timezone.utc) + timedelta(days=index)
            ).isoformat()
            rows.append(
                self._row(
                    origin_at=origin,
                    absolute_error_pct=None if index == 24 else 2.0,
                    direction_correct=None if index % 2 else 1,
                    within_q10_q90=None if index % 3 else 0,
                )
            )
            rows.append(
                self._row(
                    model_name="persistence",
                    origin_at=origin,
                    absolute_error_pct=5.0,
                )
            )
        html, _ = render_charts(rows, ["2h"], 20)

        self.assertIn("MAE % (%) · 2.00–2.00", html)
        self.assertIn("Skill vs persistence pp (pp) · 3.00–3.00", html)
        self.assertIn("current n=19", html)
        self.assertIn("low sample &lt;20", html)


if __name__ == "__main__":
    unittest.main()
