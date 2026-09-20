#!/usr/bin/env python3
"""Tests for production entrypoint instrumentation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from btc_timesfm.ops.observability import PipelineObserver
from btc_timesfm.ops.validated_entrypoints import _instrument_forecast


class ValidatedEntrypointTests(unittest.TestCase):
    def test_optional_source_retention_event_handles_report_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observer = PipelineObserver(
                report_path=root / "report.json",
                event_log_path=root / "events.jsonl",
                run_id="test-run",
            )
            forecast = SimpleNamespace(
                fetch_redundant_hourly=lambda: None,
                load_timesfm=lambda: None,
                build_forecast=lambda: None,
                evaluate_production_drift=lambda: {},
                build_experiment_manifest=lambda: {},
                evaluate_source_health=lambda: {},
                retain_optional_sources=lambda: {
                    "status": "retained",
                    "retained_record_count": 1,
                },
                ForecastHistoryStore=object,
            )
            _instrument_forecast(observer, forecast)

            report = forecast.retain_optional_sources()

            self.assertEqual(report["status"], "retained")
            events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
            event = next(
                item for item in events if item["event"] == "optional_source_inputs_retained"
            )
            self.assertEqual(event["status"], "success")
            self.assertEqual(event["retained_record_count"], 1)


if __name__ == "__main__":
    unittest.main()
