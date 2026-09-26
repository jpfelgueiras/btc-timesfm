from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class DocsPortalTests(unittest.TestCase):
    def test_homepage_and_mkdocs_navigation_use_project_pages_base(self) -> None:
        config = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
        homepage = (ROOT / "docs" / "index.md").read_text(encoding="utf-8")

        self.assertIn("site_url: https://jpfelgueiras.github.io/btc-timesfm/", config)
        self.assertIn("use_directory_urls: true", config)
        self.assertIn("- search", config)
        self.assertIn("- mermaid2:", config)
        self.assertIn("scheme: slate", config)
        self.assertIn("- Home: index.md", config)
        self.assertIn("Getting started: getting-started.md", config)
        self.assertIn(
            "Forecast dashboard](https://jpfelgueiras.github.io/btc-timesfm/forecasts/)", homepage
        )
        self.assertIn("# BTC TimesFM documentation", homepage)


if __name__ == "__main__":
    unittest.main()
