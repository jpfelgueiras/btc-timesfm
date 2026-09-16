from __future__ import annotations

import unittest

from btc_timesfm.web.charts import MAX_CHART_PAYLOAD_BYTES, render_charts


class ChartTests(unittest.TestCase):
    def _row(self, **changes: object) -> dict[str, object]:
        row: dict[str, object] = {
            "model_name": "ensemble", "origin_at": "2026-09-07T10:00:00+00:00",
            "horizon_hours": 2, "q10_usd": 98.0, "q50_usd": 100.0, "q90_usd": 102.0,
            "actual_target_price_usd": 101.0, "absolute_error_pct": 1.0,
            "direction_correct": 1, "within_q10_q90": 1, "regime": "trending",
        }
        row.update(changes)
        return row

    def test_renders_fan_and_performance_with_regime_color(self) -> None:
        html, summary = render_charts([self._row()], ["2h"], 5)

        self.assertIn("quantile fan chart", html)
        self.assertIn("rolling performance", html)
        self.assertIn("#7c9cff", html)
        self.assertIn("Chart data summary", html)
        self.assertIn("chart-low-shading", html)
        self.assertEqual(summary["2h"]["matured_samples"], 1)

    def test_degenerate_and_missing_values_are_bounded_and_have_fallback(self) -> None:
        html, _ = render_charts(
            [self._row(q10_usd=100.0, q50_usd=100.0, q90_usd=100.0, actual_target_price_usd=100.0), self._row(q10_usd=None)],
            ["2h", "4h"],
            5,
        )

        self.assertLess(len(html), MAX_CHART_PAYLOAD_BYTES)
        self.assertNotIn("nan", html.lower())
        self.assertNotIn("inf", html.lower())
        self.assertIn("No matured forecasts with q10", html)

    def test_escapes_untrusted_regime_text(self) -> None:
        html, _ = render_charts([self._row(regime='<script>alert(1)</script>')], ["2h"], 1)

        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


if __name__ == "__main__":
    unittest.main()
