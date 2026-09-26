from __future__ import annotations

import functools
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from btc_timesfm.web.historical_explorer import build_explorer_data
from btc_timesfm.web.static_site import render_html


def _page_fixture() -> str:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    rows = []
    for age, horizon, model, actual in (
        (timedelta(days=20), 2, "ensemble", 101.0),
        (timedelta(days=1), 4, "ensemble", 102.0),
        (timedelta(hours=1), 2, "persistence", None),
    ):
        origin = now - age
        target = origin + timedelta(hours=horizon)
        rows.append(
            {
                "origin_at": origin.isoformat(),
                "target_at": target.isoformat(),
                "horizon_hours": horizon,
                "model_name": model,
                "source_price_usd": 100.0,
                "predicted_price_usd": 102.0,
                "predicted_change_pct": 2.0,
                "q10_usd": 99.0,
                "q50_usd": 102.0,
                "q90_usd": 105.0,
                "actual_target_price_usd": actual,
                "absolute_error_pct": 1.0 if actual is not None else None,
                "signed_error_pct": 1.0 if actual is not None else None,
                "actual_change_pct": 1.0 if actual is not None else None,
                "direction_correct": 1 if actual is not None else None,
                "within_q10_q90": 1 if actual is not None else None,
                "experiment_manifest_json": '{"configuration_id":"fixture-config"}',
            }
        )
    windows = {
        name: {
            "2h": {
                "samples": 1,
                "mae_pct": 1.0,
                "direction_accuracy": 1.0,
                "q10_q90_coverage": 1.0,
                "confidence_warning": None,
            }
        }
        for name in ("7d", "30d", "90d", "all")
    }
    empty_window = {"by_horizon": {}, "by_regime": {}, "by_volatility_bucket": {}}
    data = {
        "generated_at": now.isoformat(),
        "latest": None,
        "latest_age_hours": None,
        "accuracy": windows,
        "horizons": ["2h", "4h"],
        "low_sample_threshold": 5,
        "chart_rows": [
            {
                "model_name": "ensemble",
                "origin_at": rows[0]["origin_at"],
                "horizon_hours": 2,
                "q10_usd": 99.0,
                "q50_usd": 102.0,
                "q90_usd": 105.0,
                "actual_target_price_usd": 101.0,
                "absolute_error_pct": 1.0,
                "within_q10_q90": 1,
                "direction_correct": 1,
            },
            {
                "model_name": "persistence",
                "origin_at": rows[0]["origin_at"],
                "horizon_hours": 2,
                "absolute_error_pct": 2.0,
            },
        ],
        "persistence_edge": {
            "low_sample_threshold": 5,
            "windows": {name: empty_window for name in ("7d", "30d", "90d", "all")},
        },
        "explorer": {"horizons": [], "rows": []},
        "historical_explorer": build_explorer_data(rows, now=now),
        "recent": [],
        "matured_rows": 2,
    }
    return render_html(data)


def _empty_fixture() -> str:
    now = datetime.now(timezone.utc)
    empty_window = {"by_horizon": {}, "by_regime": {}, "by_volatility_bucket": {}}
    return render_html(
        {
            "generated_at": now.isoformat(),
            "latest": None,
            "latest_age_hours": None,
            "accuracy": {key: {} for key in ("7d", "30d", "90d", "all")},
            "horizons": [],
            "low_sample_threshold": 5,
            "chart_rows": [],
            "persistence_edge": {
                "low_sample_threshold": 5,
                "windows": {key: empty_window for key in ("7d", "30d", "90d", "all")},
            },
            "explorer": {"horizons": [], "rows": []},
            "historical_explorer": build_explorer_data([], now=now),
            "recent": [],
            "matured_rows": 0,
        }
    )


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


class BrowserAccessibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        forecasts_dir = Path(cls.temp_dir.name, "forecasts")
        forecasts_dir.mkdir()
        Path(forecasts_dir, "index.html").write_text(_page_fixture(), encoding="utf-8")
        Path(forecasts_dir, "empty.html").write_text(_empty_fixture(), encoding="utf-8")
        handler = functools.partial(_QuietHandler, directory=cls.temp_dir.name)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/forecasts/index.html"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)
        cls.temp_dir.cleanup()

    def _page(self, page: Page, query: str = "") -> None:
        page.goto(self.base_url + query, wait_until="load")

    def test_target_viewports_themes_and_table_reflow(self) -> None:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            self._page(page, "?days=7&horizon=2#explorer")
            self.assertTrue(
                page.get_by_role("tab", name="Forecast History").get_attribute("aria-selected")
                == "true"
            )
            self.assertEqual(page.get_by_text("1 of 3 forecasts").count(), 1)
            self.assertEqual(page.get_by_label("Horizon").input_value(), "2")
            self.assertEqual(page.get_by_label("Date range").input_value(), "7")

            for width, height in ((375, 812), (720, 900), (768, 1024), (1440, 900)):
                page.set_viewport_size({"width": width, "height": height})
                overflow = page.evaluate(
                    "({page:document.documentElement.scrollWidth>innerWidth,table:document.querySelector('#history-table').parentElement.scrollWidth>document.querySelector('#history-table').parentElement.clientWidth})"
                )
                self.assertFalse(overflow["page"], f"page overflow at {width}px")
                wrapper = page.get_by_role("region", name="Scrollable forecast history table")
                self.assertEqual(wrapper.get_attribute("tabindex"), "0")
                wrapper.focus()
                self.assertGreaterEqual(
                    page.evaluate(
                        "parseFloat(getComputedStyle(document.activeElement).outlineWidth)"
                    ),
                    3.0,
                )
                if width <= 768:
                    self.assertTrue(overflow["table"], f"expected local table scroll at {width}px")
                maturity = page.get_by_label("Maturity")
                maturity.focus()
                page.keyboard.press("m")
                page.keyboard.press("Enter")
                self.assertEqual(maturity.input_value(), "matured")
                self.assertEqual(page.get_by_text("0 of 3 forecasts").count(), 1)
                self.assertTrue(page.get_by_text("No forecasts match these filters").is_visible())
                page.go_back()
                self.assertEqual(page.get_by_label("Maturity").input_value(), "")
                page.get_by_label("Maturity").select_option("")

                page.get_by_role("tab", name="Overview").click()
                self.assertFalse(
                    page.evaluate("document.documentElement.scrollWidth>innerWidth"),
                    f"overview overflow at {width}px",
                )
                page.get_by_role("tab", name="Model Metrics").click()
                page.get_by_role("tab", name="Quantile fans & performance trends").click()
                self.assertFalse(
                    page.evaluate("document.documentElement.scrollWidth>innerWidth"),
                    f"metrics overflow at {width}px",
                )
                page.get_by_role("tab", name="Forecast History").click()

            for theme in ("light", "dark"):
                page.evaluate(
                    "(theme)=>document.documentElement.setAttribute('data-theme',theme)", theme
                )
                screenshot_dir = Path(
                    os.environ.get(
                        "DASHBOARD_SCREENSHOT_DIR", Path(self.temp_dir.name, "screenshots")
                    )
                )
                screenshot_dir.mkdir(parents=True, exist_ok=True)
                screenshot = screenshot_dir / f"forecast-history-{width}x{height}-{theme}.png"
                page.screenshot(path=str(screenshot), full_page=True)
                self.assertGreater(screenshot.stat().st_size, 1000)
                token_contrast = page.evaluate(
                    """() => {
                      const css=getComputedStyle(document.documentElement),
                        vars=['--muted','--text','--green','--red','--amber','--blue'];
                      const rgb=value=>value.startsWith('#')?value.slice(1).match(/../g).map(v=>parseInt(v,16)):value.match(/[\\d.]+/g).slice(0,3).map(Number);
                      const lum=color=>{const channels=rgb(color).map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4);return .2126*channels[0]+.7152*channels[1]+.0722*channels[2]};
                      const contrast=(a,b)=>{if(a==='none'||a==='rgba(0, 0, 0, 0)')return 1;const x=lum(a),y=lum(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05)};
                      const bg=css.getPropertyValue('--bg').trim(),panel=css.getPropertyValue('--panel').trim();
                      return vars.map(key=>({key,ratio:Math.min(contrast(css.getPropertyValue(key).trim(),bg),contrast(css.getPropertyValue(key).trim(),panel))}));
                    }"""
                )
                self.assertGreaterEqual(min(entry["ratio"] for entry in token_contrast), 4.5)
                graphic_contrast = page.evaluate(
                    """() => {
                      const rgb=value=>value.startsWith('#')?value.slice(1).match(/../g).map(v=>parseInt(v,16)):(value.match(/[\\d.]+/g)||[0,0,0]).slice(0,3).map(Number);
                      const lum=color=>{const channels=rgb(color).map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4);return .2126*channels[0]+.7152*channels[1]+.0722*channels[2]};
                      const contrast=(a,b)=>{if(a==='none'||a==='rgba(0, 0, 0, 0)')return 1;const x=lum(a),y=lum(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05)};
                      const panel=getComputedStyle(document.documentElement).getPropertyValue('--panel').trim();
                      return [...document.querySelectorAll('.fan-band,.fan-median,.fan-actual,.chart-actual,.performance-mae,.performance-direction,.performance-coverage,.performance-skill,.chart-low-sample')]
                        .map(el=>{const style=getComputedStyle(el);return {className:el.getAttribute('class'),fill:style.fill,stroke:style.stroke,ratio:Math.max(contrast(style.fill,panel),contrast(style.stroke,panel))}});
                    }"""
                )
                self.assertGreaterEqual(
                    min(entry["ratio"] for entry in graphic_contrast), 3.0, str(graphic_contrast)
                )
            browser.close()

    def test_keyboard_touch_controls_accessible_names_and_chart_alternative(self) -> None:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 375, "height": 812}, has_touch=True)
            self._page(page)
            overview = page.get_by_role("tab", name="Overview")
            overview.focus()
            page.keyboard.press("ArrowRight")
            self.assertEqual(
                page.evaluate("document.activeElement.textContent.trim()"), "Forecast History"
            )
            self.assertFalse(page.evaluate("document.activeElement.closest('[hidden]') !== null"))
            page.get_by_role("tab", name="Forecast History").tap()
            model = page.locator("#history-model")
            model.focus()
            page.keyboard.press("p")
            page.keyboard.press("Enter")
            self.assertEqual(model.input_value(), "persistence")
            self.assertEqual(page.get_by_text("1 of 3 forecasts").count(), 1)
            page.reload()
            self.assertEqual(page.locator("#history-model").input_value(), "persistence")
            self.assertEqual(page.get_by_text("1 of 3 forecasts").count(), 1)
            page.get_by_role("tab", name="Model Metrics").tap()
            page.get_by_role("tab", name="Quantile fans & performance trends").tap()
            chart = page.get_by_role("img", name="2h quantile fan chart")
            self.assertIn(
                "Each point is a matured forecast", chart.locator("desc").text_content() or ""
            )

            page.get_by_role("tab", name="Overview").tap()
            page.get_by_role("button", name="Toggle light/dark theme").tap()
            self.assertIn(page.locator("html").get_attribute("data-theme"), ("light", "dark"))
            audit = page.evaluate(
                """() => {
                  const issues=[];
                  for(const el of document.querySelectorAll('button,[role=tab],a,select,input,summary')){
                    const label=el.getAttribute('aria-label')||el.getAttribute('title')||el.innerText||el.labels?.[0]?.innerText;
                    if(!label?.trim())issues.push('unnamed '+el.tagName);
                  }
                  for(const svg of document.querySelectorAll('svg[role=img]'))
                    if(!svg.getAttribute('aria-labelledby')||!svg.querySelector('title,desc'))issues.push('unlabeled chart');
                  for(const table of document.querySelectorAll('table'))
                    if(!table.querySelector('thead th[scope=col]'))issues.push('table without scoped column headers');
                  const ids=[...document.querySelectorAll('[id]')].map(el=>el.id);
                  if(new Set(ids).size!==ids.length)issues.push('duplicate id');
                  for(const tab of document.querySelectorAll('[role=tab]')){
                    const panel=document.getElementById(tab.getAttribute('aria-controls'));
                    if(!panel||panel.getAttribute('aria-labelledby')!==tab.id)issues.push('disconnected tab/panel');
                  }
                  return issues;
                }"""
            )
            self.assertEqual(audit, [])
            browser.close()

    def test_empty_and_no_javascript_fallback_states_in_browser(self) -> None:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 375, "height": 812})
            page.goto(self.base_url.replace("index.html", "empty.html"))
            self.assertTrue(page.get_by_text("No forecast history is available yet").is_visible())
            page.get_by_role("tab", name="Forecast History").click()
            self.assertTrue(page.get_by_text("No historical forecasts are available").is_visible())

            no_js = browser.new_page(java_script_enabled=False)
            self._page(no_js, "?days=7&horizon=2#explorer")
            self.assertEqual(no_js.locator("#history-table tbody tr").count(), 3)
            self.assertTrue(no_js.get_by_text("Filters need JavaScript").is_visible())
            self.assertTrue(
                no_js.get_by_role("heading", name="Forecast History Explorer").is_visible()
            )
            browser.close()


if __name__ == "__main__":
    unittest.main()
