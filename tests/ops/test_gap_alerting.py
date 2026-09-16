#!/usr/bin/env python3
"""Unit tests for gap-threshold alerting and observability integration."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from btc_timesfm.ops.gap_alerting import (
    GAP_ALERT_SCHEMA_VERSION,
    GapAlertConfig,
    GapAlertMonitor,
    build_status,
    evaluate_gap_alerts,
    publish_gap_alerts,
)

from btc_timesfm.ops.observability import PipelineObserver


NOW = datetime(2026, 9, 6, 18, 0, 0, tzinfo=timezone.utc)


def _ts(*, relative_hours: int) -> int:
    return int((NOW - timedelta(hours=relative_hours)).timestamp())


CLEAN_REPORT = {
    "summary": {
        "gaps_detected": 0,
        "missing_candles_total": 0,
        "silent_fill_candidates": 0,
        "withhold_gaps": 0,
    }
}


def _report(**overrides: int) -> dict[str, Any]:
    base = {
        "gaps_detected": 0,
        "missing_candles_total": 0,
        "silent_fill_candidates": 0,
        "withhold_gaps": 0,
    }
    base.update(overrides)
    return {"summary": base}


class GapAlertConfigTests(unittest.TestCase):
    def test_default_config(self) -> None:
        config = GapAlertConfig()
        self.assertEqual(config.warning_gaps_detected, 3)
        self.assertEqual(config.critical_gaps_detected, 6)

    def test_negative_warning_raises(self) -> None:
        with self.assertRaises(ValueError):
            GapAlertConfig(warning_gaps_detected=-1)

    def test_critical_below_warning_raises(self) -> None:
        with self.assertRaises(ValueError):
            GapAlertConfig(critical_gaps_detected=2, warning_gaps_detected=3)

    def test_dedup_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            GapAlertConfig(alert_dedup_minutes=0)

    def test_as_dict_round_trip(self) -> None:
        config = GapAlertConfig()
        as_dict = config.as_dict()
        self.assertEqual(as_dict["warning_gaps_detected"], 3)
        self.assertEqual(as_dict["alert_dedup_minutes"], 60)


class EvaluateGapAlertsTests(unittest.TestCase):
    def test_no_alerts_within_thresholds(self) -> None:
        evaluation = evaluate_gap_alerts(
            _report(gaps_detected=1, missing_candles_total=1),
            config=GapAlertConfig(),
        )
        self.assertEqual(evaluation["level"], "ok")
        self.assertEqual(evaluation["alerts"], [])

    def test_warning_when_thresholds_reached(self) -> None:
        evaluation = evaluate_gap_alerts(
            _report(gaps_detected=4),
            config=GapAlertConfig(),
        )
        self.assertEqual(evaluation["level"], "warning")
        self.assertGreater(len(evaluation["alerts"]), 0)
        alert = evaluation["alerts"][0]
        self.assertEqual(alert["level"], "warning")
        self.assertEqual(alert["metric"], "gaps_detected")

    def test_critical_when_critical_threshold_reached(self) -> None:
        evaluation = evaluate_gap_alerts(
            _report(gaps_detected=6),
            config=GapAlertConfig(),
        )
        self.assertEqual(evaluation["level"], "critical")
        alert = evaluation["alerts"][0]
        self.assertEqual(alert["level"], "critical")

    def test_critical_overrides_warning(self) -> None:
        evaluation = evaluate_gap_alerts(
            _report(gaps_detected=4, silent_fill_candidates=6),
            config=GapAlertConfig(),
        )
        self.assertEqual(evaluation["level"], "critical")

    def test_silent_fill_warning(self) -> None:
        evaluation = evaluate_gap_alerts(
            _report(silent_fill_candidates=3),
            config=GapAlertConfig(),
        )
        self.assertEqual(evaluation["level"], "warning")

    def test_withhold_metric_always_critical_when_nonzero(self) -> None:
        evaluation = evaluate_gap_alerts(
            _report(withhold_gaps=1),
            config=GapAlertConfig(),
        )
        self.assertEqual(evaluation["level"], "critical")
        withheld_alerts = [a for a in evaluation["alerts"] if a["metric"] == "withhold_gaps"]
        self.assertTrue(withheld_alerts)
        self.assertEqual(withheld_alerts[0]["level"], "critical")

    def test_withhold_metric_zero_does_not_alert(self) -> None:
        config = GapAlertConfig(warning_withhold_gaps=0, critical_withhold_gaps=1)
        evaluation = evaluate_gap_alerts(
            _report(withhold_gaps=0),
            config=config,
        )
        self.assertEqual(evaluation["level"], "ok")

    def test_policy_withhold_forecast_is_reflected(self) -> None:
        report = _report(gaps_detected=1)
        report["summary"]["policy"] = {"withhold_forecast": True}
        evaluation = evaluate_gap_alerts(report, config=GapAlertConfig())
        self.assertTrue(evaluation["policy_withhold_forecast"])
        self.assertEqual(evaluation["level"], "critical")

    def test_invalid_report_returns_ok(self) -> None:
        evaluation = evaluate_gap_alerts("bad")
        self.assertEqual(evaluation["level"], "ok")

    def test_schema_version(self) -> None:
        evaluation = evaluate_gap_alerts(_report())
        self.assertEqual(evaluation["schema_version"], GAP_ALERT_SCHEMA_VERSION)


class PublishGapAlertsTests(unittest.TestCase):
    def test_no_alerts_fires_health_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observer = PipelineObserver(
                report_path=root / "report.json",
                event_log_path=root / "events.jsonl",
                run_id="test-gap",
            )
            publish_gap_alerts(observer, CLEAN_REPORT, now=NOW)
            events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
            status_events = [e for e in events if e.get("event") == "gap_health"]
            self.assertTrue(status_events)
            self.assertEqual(status_events[0]["status"], "ok")

    def test_alerts_fire_and_increment_counters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observer = PipelineObserver(
                report_path=root / "report.json",
                event_log_path=root / "events.jsonl",
                run_id="test-gap-alerts",
            )
            report = _report(gaps_detected=6)
            publish_gap_alerts(observer, report, now=NOW)
            events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
            gap_alert_events = [e for e in events if e.get("event") == "gap_alert"]
            self.assertTrue(gap_alert_events)
            self.assertEqual(gap_alert_events[0]["status"], "critical")
            self.assertEqual(observer.data["counters"].get("data_quality_events"), 1)
            self.assertEqual(observer.data["counters"].get("gap_alerts"), 1)

    def test_returns_evaluation_dict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            observer = PipelineObserver(
                report_path=Path(directory) / "report.json",
                event_log_path=Path(directory) / "events.jsonl",
                run_id="test-gap-eval",
            )
            evaluation = publish_gap_alerts(observer, _report(gaps_detected=6), now=NOW)
            self.assertIn("level", evaluation)
            self.assertIn("metrics", evaluation)


class BuildStatusTests(unittest.TestCase):
    def test_status_written_and_returned(self) -> None:
        evaluation = evaluate_gap_alerts(_report(), config=GapAlertConfig())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            status = build_status(
                {"summary": {"gaps_detected": 0}},
                evaluation,
                status_path=path,
            )
            self.assertEqual(status["level"], "ok")
            self.assertTrue(path.exists())
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["level"], "ok")


class GapAlertMonitorTests(unittest.TestCase):
    def test_dedup_suppresses_second_firing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            monitor = GapAlertMonitor(
                state_path=Path(directory) / "state.json",
                log_path=Path(directory) / "events.jsonl",
                config=GapAlertConfig(
                    warning_gaps_detected=1, critical_gaps_detected=2, alert_dedup_minutes=10
                ),
            )
            report = _report(gaps_detected=1)
            first = monitor.record(report, now=NOW)
            second = monitor.record(report, now=NOW + timedelta(minutes=5))
            self.assertEqual(len(first["alerts_emitted"]), 1)
            self.assertEqual(len(second["alerts_emitted"]), 0)

    def test_recovery_and_re_alert(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            monitor = GapAlertMonitor(
                state_path=Path(directory) / "state.json",
                log_path=Path(directory) / "events.jsonl",
                config=GapAlertConfig(
                    warning_gaps_detected=1, critical_gaps_detected=2, alert_dedup_minutes=10
                ),
            )
            first = monitor.record(_report(gaps_detected=1), now=NOW)
            second = monitor.record(_report(gaps_detected=0), now=NOW + timedelta(minutes=1))
            third = monitor.record(_report(gaps_detected=1), now=NOW + timedelta(minutes=30))
            self.assertEqual(len(first["alerts_emitted"]), 1)
            self.assertEqual(len(second["alerts_emitted"]), 0)
            self.assertEqual(len(third["alerts_emitted"]), 1)


class ConfigFromEnvTests(unittest.TestCase):
    def test_env_int_value_parsed(self) -> None:
        with patch.dict(
            "os.environ",
            {"BTC_GAP_ALERT_WARN_GAPS": "7", "BTC_GAP_ALERT_CRITICAL_GAPS": "9"},
        ):
            config = GapAlertConfig.from_env()
            self.assertEqual(config.warning_gaps_detected, 7)
            self.assertEqual(config.critical_gaps_detected, 9)


if __name__ == "__main__":
    unittest.main()
