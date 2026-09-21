from __future__ import annotations

import unittest
from html.parser import HTMLParser

from btc_timesfm.web.charts import render_charts
from btc_timesfm.web.static_site import _explorer_script, render_html


class _DashboardParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, dict(attrs)))


class UiRegressionTests(unittest.TestCase):
    def _page(self) -> str:
        return render_html(
            {
                "generated_at": "2026-09-20T00:00:00+00:00",
                "latest": None,
                "latest_age_hours": None,
                "accuracy": {"7d": {}, "30d": {}, "90d": {}, "all": {}},
                "horizons": [],
                "low_sample_threshold": 5,
                "chart_rows": [],
                "persistence_edge": {"low_sample_threshold": 5, "windows": {}},
                "explorer": {"horizons": [], "rows": []},
                "historical_explorer": {},
                "recent": [],
                "matured_rows": 0,
            }
        )

    def test_tabs_keep_controls_and_panels_connected(self) -> None:
        parser = _DashboardParser()
        parser.feed(self._page())
        controls = {
            attrs["id"]: attrs
            for tag, attrs in parser.elements
            if tag == "button" and attrs.get("role") == "tab"
        }
        panels = {
            attrs["id"]: attrs
            for tag, attrs in parser.elements
            if tag == "div" and attrs.get("role") == "tabpanel"
        }

        self.assertEqual(
            set(controls), {"tab-overview-button", "tab-explorer-button", "tab-metrics-button"}
        )
        self.assertEqual(set(panels), {"tab-overview", "tab-explorer", "tab-metrics"})
        for control in controls.values():
            panel_id = control["aria-controls"]
            self.assertIn(panel_id, panels)
            self.assertEqual(panels[panel_id]["aria-labelledby"], control["id"])
        self.assertNotIn("hidden", panels["tab-overview"])
        self.assertIn("hidden", panels["tab-explorer"])
        self.assertIn("hidden", panels["tab-metrics"])

    def test_explorer_script_handles_search_input_and_metadata(self) -> None:
        script = _explorer_script()

        self.assertIn("el instanceof HTMLSelectElement", script)
        self.assertIn("r.dataset.regime", script)
        self.assertIn("r.dataset.status", script)
        self.assertIn('el.addEventListener("input",apply)', script)

    def test_charts_are_responsive_svg_images(self) -> None:
        charts, _ = render_charts([], ["2h"], 5)

        self.assertIn('<svg class="chart" width="760" height="260"', charts)
        self.assertIn('preserveAspectRatio="xMidYMid meet"', charts)
        self.assertNotIn('height="auto"', charts)
        self.assertIn('role="img"', charts)
        self.assertIn("No matured forecasts", charts)


if __name__ == "__main__":
    unittest.main()
