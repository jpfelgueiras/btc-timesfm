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
    ) -> dict[str, object]:
        return {
            "generated_at": origin,
            "origin_at": origin,
            "source_name": "kraken",
            "pair": "BTC/USD",
            "source_price_usd": 100.0,
            "regime": "range",
            "model_name": "ensemble",
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

        self.assertIn("Forecasts &amp; accuracy", page)
        self.assertIn("$102", page)
        self.assertIn("Recent forecast ledger", page)
        self.assertIn("No X/Twitter dependency", page)
        self.assertIn("Pending", page)


if __name__ == "__main__":
    unittest.main()
