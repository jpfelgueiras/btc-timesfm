#!/usr/bin/env python3
"""Tests for site output contract validation."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from btc_timesfm.web.contract_validation import (
    SCHEMA_VERSION,
    validate_data_json,
    validate_site_output,
)
from btc_timesfm.web.static_site import build_site_data, render_html


def _make_valid_data(*, schema_version: int = SCHEMA_VERSION) -> dict:
    """Build a minimal valid data.json payload."""
    return {
        "schema_version": schema_version,
        "generated_at": "2026-09-07T13:00:00+00:00",
        "latest": None,
        "latest_age_hours": 1.0,
        "accuracy": {"7d": {}, "30d": {}, "90d": {}, "all": {}},
        "persistence_edge": {
            "low_sample_threshold": 5,
            "horizons": ["2h", "4h"],
            "windows": {
                "7d": {"days": 7, "matured_rows": 10, "paired_samples": 5, "by_horizon": {}, "by_regime": {}, "by_volatility_bucket": {}},
                "30d": {"days": 30, "matured_rows": 20, "paired_samples": 10, "by_horizon": {}, "by_regime": {}, "by_volatility_bucket": {}},
                "90d": {"days": 90, "matured_rows": 30, "paired_samples": 15, "by_horizon": {}, "by_regime": {}, "by_volatility_bucket": {}},
                "all": {"days": None, "matured_rows": 50, "paired_samples": 25, "by_horizon": {}, "by_regime": {}, "by_volatility_bucket": {}},
            },
            "reproducibility": {},
        },
        "recent": [],
        "matured_rows": 50,
        "horizons": ["2h", "4h"],
        "low_sample_threshold": 5,
        "multi_horizon_coherence": None,
        "database_verification": {"integrity": "ok", "schema_version": 1, "supported_schema_version": 1},
    }


def _make_valid_html() -> str:
    """Build minimal valid HTML matching the site contract."""
    return (
        "<!doctype html>\n"
        "<html lang=\"en\">\n"
        "<head><meta charset=\"utf-8\"><title>BTC TimesFM Forecasts</title></head>\n"
        "<body>\n"
        "<main>\n"
        "<section><h1>Forecasts & accuracy</h1></section>\n"
        "<section><h2>Accuracy</h2></section>\n"
        "<section><h2>Ensemble edge vs persistence</h2></section>\n"
        "<section><h2>Recent forecast ledger</h2></section>\n"
        "</main>\n"
        "</body>\n"
        "</html>\n"
    )


class TestValidateDataJson(unittest.TestCase):
    def test_valid_data_passes(self) -> None:
        errors = validate_data_json(_make_valid_data())
        self.assertEqual(errors, [])

    def test_missing_required_key(self) -> None:
        data = _make_valid_data()
        del data["matured_rows"]
        errors = validate_data_json(data)
        self.assertTrue(any("matured_rows" in e for e in errors))

    def test_wrong_type(self) -> None:
        data = _make_valid_data()
        data["matured_rows"] = "not_an_int"
        errors = validate_data_json(data)
        self.assertTrue(any("matured_rows" in e for e in errors))

    def test_schema_version_mismatch(self) -> None:
        data = _make_valid_data(schema_version=999)
        errors = validate_data_json(data)
        self.assertTrue(any("schema_version" in e for e in errors))

    def test_negative_latest_age_hours(self) -> None:
        data = _make_valid_data()
        data["latest_age_hours"] = -1.0
        errors = validate_data_json(data)
        self.assertTrue(any("negative" in e for e in errors))

    def test_unreasonably_large_latest_age_hours(self) -> None:
        data = _make_valid_data()
        data["latest_age_hours"] = 200.0
        errors = validate_data_json(data)
        self.assertTrue(any("unreasonably large" in e for e in errors))

    def test_negative_matured_rows(self) -> None:
        data = _make_valid_data()
        data["matured_rows"] = -5
        errors = validate_data_json(data)
        self.assertTrue(any("negative" in e for e in errors))

    def test_invalid_generated_at_timestamp(self) -> None:
        data = _make_valid_data()
        data["generated_at"] = "not-a-timestamp"
        errors = validate_data_json(data)
        self.assertTrue(any("generated_at" in e for e in errors))

    def test_placeholder_marker_in_data(self) -> None:
        data = _make_valid_data()
        data["recent"] = [{"status": "TODO implement"}]
        errors = validate_data_json(data)
        self.assertTrue(any("Placeholder" in e for e in errors))

    def test_placeholder_error_marker(self) -> None:
        data = _make_valid_data()
        data["recent"] = [{"status": "ERROR"}]
        errors = validate_data_json(data)
        self.assertTrue(any("ERROR marker" in e for e in errors))

    def test_null_placeholder_marker(self) -> None:
        data = _make_valid_data()
        data["recent"] = [{"status": "null"}]
        errors = validate_data_json(data)
        self.assertTrue(any("Placeholder" in e for e in errors))


class TestValidateSiteOutput(unittest.TestCase):
    def _write_site(self, html: str, data: dict) -> Path:
        tmpdir = Path(self._tmpdir.name)
        (tmpdir / "index.html").write_text(html, encoding="utf-8")
        (tmpdir / "data.json").write_text(
            json.dumps(data, sort_keys=True) + "\n", encoding="utf-8"
        )
        return tmpdir

    def setUp(self) -> None:
        self._tmpdir = TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)

    def test_valid_site_passes(self) -> None:
        result = validate_site_output(self._write_site(_make_valid_html(), _make_valid_data()))
        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["schema_version"], SCHEMA_VERSION)

    def test_missing_index_html(self) -> None:
        tmpdir = Path(self._tmpdir.name)
        (tmpdir / "data.json").write_text(json.dumps(_make_valid_data()), encoding="utf-8")
        result = validate_site_output(tmpdir)
        self.assertFalse(result["passed"])
        self.assertTrue(any("Missing" in e for e in result["errors"]))

    def test_missing_data_json(self) -> None:
        tmpdir = Path(self._tmpdir.name)
        (tmpdir / "index.html").write_text(_make_valid_html(), encoding="utf-8")
        result = validate_site_output(tmpdir)
        self.assertFalse(result["passed"])
        self.assertTrue(any("Missing" in e for e in result["errors"]))

    def test_malformed_html_fails(self) -> None:
        result = validate_site_output(self._write_site("<p>broken</p>", _make_valid_data()))
        self.assertFalse(result["passed"])
        self.assertTrue(any("doctype" in e or "Missing" in e for e in result["errors"]))

    def test_html_missing_required_section(self) -> None:
        bad_html = (
            "<!doctype html>\n<html lang=\"en\">\n<head></head>\n"
            "<body><main><h1>Forecasts &amp; accuracy</h1>"
            "</main></body></html>"
        )
        result = validate_site_output(self._write_site(bad_html, _make_valid_data()))
        self.assertFalse(result["passed"])
        self.assertTrue(any("Accuracy" in e and "missing" in e.lower() for e in result["errors"]))

    def test_html_placeholder_marker_fails(self) -> None:
        html_with_todo = _make_valid_html().replace(
            "</main>", "<div>TODO add footer</div></main>"
        )
        result = validate_site_output(self._write_site(html_with_todo, _make_valid_data()))
        self.assertFalse(result["passed"])
        self.assertTrue(any("Placeholder" in e or "placeholder" in e for e in result["errors"]))

    def test_invalid_data_json_syntax(self) -> None:
        tmpdir = Path(self._tmpdir.name)
        (tmpdir / "index.html").write_text(_make_valid_html(), encoding="utf-8")
        (tmpdir / "data.json").write_text("{invalid json}", encoding="utf-8")
        result = validate_site_output(tmpdir)
        self.assertFalse(result["passed"])
        self.assertTrue(any("parse" in e.lower() for e in result["errors"]))

    def test_schema_version_in_report(self) -> None:
        result = validate_site_output(self._write_site(_make_valid_html(), _make_valid_data()))
        self.assertEqual(result["schema_version"], SCHEMA_VERSION)


class TestContractFromGenerator(unittest.TestCase):
    """Validate that the real site generator output passes the contract."""

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
    ) -> dict:
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

    def test_generated_output_passes_contract(self) -> None:
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
        ]
        data = build_site_data(
            rows, now=datetime(2026, 9, 7, 13, tzinfo=timezone.utc)
        )
        data["database_verification"] = {
            "integrity": "ok",
            "schema_version": 1,
            "supported_schema_version": 1,
        }
        html = render_html(data)

        with TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            (out / "index.html").write_text(html, encoding="utf-8")
            (out / "data.json").write_text(
                json.dumps(data, sort_keys=True) + "\n", encoding="utf-8"
            )
            result = validate_site_output(out)

        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["schema_version"], SCHEMA_VERSION)
        self.assertEqual(data["schema_version"], SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
