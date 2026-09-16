from __future__ import annotations

import unittest

from btc_timesfm.web.static_site import render_html


class SiteAccessibilityTests(unittest.TestCase):
    def test_accessibility_elements(self) -> None:
        data = {
            "generated_at": "2026-09-07T13:00:00+00:00",
            "latest": None,
            "latest_age_hours": None,
            "accuracy": {"all": {}},
            "horizons": [],
            "low_sample_threshold": 10,
            "chart_rows": [],
            "persistence_edge": {"low_sample_threshold": 10, "windows": {}},
            "explorer": {"horizons": [], "rows": []},
            "recent": [],
            "matured_rows": 0,
        }

        html_content = render_html(data)

        self.assertIn('<nav class="site-nav" aria-label="Page sections">', html_content)
        self.assertIn('role="main"', html_content)
        self.assertIn('role="contentinfo"', html_content)
        self.assertIn('class="skip-link"', html_content)
        self.assertIn('class="theme-toggle"', html_content)


if __name__ == "__main__":
    unittest.main()
