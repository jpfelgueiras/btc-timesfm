from __future__ import annotations

import unittest
from datetime import datetime, timezone
from btc_timesfm.web.static_site import build_site_data, render_html


class RichFieldsTests(unittest.TestCase):
    def _row(
        self,
        *,
        origin: str,
        horizon: int,
        predicted: float,
        change: float,
    ) -> dict[str, object]:
        return {
            "origin_at": origin,
            "horizon_hours": horizon,
            "target_at": f"2026-09-07T{12 + horizon:02d}:00:00+00:00",
            "source_price_usd": 100.0,
            "predicted_price_usd": predicted,
            "predicted_change_pct": change,
            "model_name": "ensemble",
        }

    def test_rich_fields_render(self) -> None:
        # Mocking the rich fields in latest_snapshot
        snapshot = {
            "direction_probability": {
                "horizons": {"2h": {"p_up": 0.6, "p_down": 0.4, "p_move": 0.5}}
            },
            "dynamic_thresholds": {"horizons": {"2h": {"edge_status": "edge"}}},
            "abstention_policy": {"state": "healthy"},
        }

        row = self._row(origin="2026-09-07T12:00:00+00:00", horizon=2, predicted=101.0, change=1.0)

        data = build_site_data(
            [row],
            now=datetime(2026, 9, 7, 13, tzinfo=timezone.utc),
            latest_snapshot=snapshot,
        )

        # Verify rich fields are in data
        latest = data["latest"]
        self.assertEqual(latest["predictions"][0]["direction_probability"]["p_up"], 0.6)

        # Verify they render in HTML
        page = render_html(data)
        self.assertIn("P(up):", page)
        self.assertIn("60%", page)
        self.assertNotIn("healthy", page)

    def test_abstention_renders(self) -> None:
        snapshot = {"abstention_policy": {"state": "degraded"}}
        row = self._row(origin="2026-09-07T12:00:00+00:00", horizon=2, predicted=101.0, change=1.0)
        data = build_site_data([row], latest_snapshot=snapshot)
        page = render_html(data)
        self.assertIn("degraded", page)

    def test_attribution_renders(self) -> None:
        snapshot = {"attribution": {"explanation": "Test explanation"}}
        row = self._row(origin="2026-09-07T12:00:00+00:00", horizon=2, predicted=101.0, change=1.0)
        data = build_site_data([row], latest_snapshot=snapshot)
        page = render_html(data)
        self.assertIn("Test explanation", page)
