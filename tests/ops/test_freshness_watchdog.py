#!/usr/bin/env python3
"""Tests for freshness SLO watchdog module."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta, timezone
from pathlib import Path

from btc_timesfm.ops.freshness_slo import (
    FreshnessMetric,
    FreshnessSLOConfig,
    _iso,
)
from btc_timesfm.ops.freshness_watchdog import (
    BreachSeverity,
    CheckResult,
    _dedup_key,
    _format_alert_message,
    _format_recovery_message,
    _latest_backup_time,
    _latest_history_origin,
    _latest_scheduled_run,
    _load_state,
    _save_state,
    _severity_for_age,
    _should_alert,
    check_history_backup,
    check_production_forecast,
    check_scheduled_run,
    check_site_update,
    compute_weekly_slo_adherence,
    run_all_checks,
)
from btc_timesfm.ops.freshness_slo import _utc_now


UTC = timezone.utc

FORECAST_SLO_CONFIG = FreshnessSLOConfig(
    metrics=(
        FreshnessMetric(
            name="production_forecast",
            description="Forecast within 3h",
            target_hours=1.0,
            warning_tolerance_hours=2.0,
            critical_tolerance_hours=3.0,
        ),
        FreshnessMetric(
            name="site_update",
            description="Site within 2h",
            target_hours=1.0,
            warning_tolerance_hours=1.5,
            critical_tolerance_hours=2.0,
        ),
        FreshnessMetric(
            name="history_backup",
            description="Backup within 24h",
            target_hours=12.0,
            warning_tolerance_hours=20.0,
            critical_tolerance_hours=24.0,
        ),
        FreshnessMetric(
            name="scheduled_run",
            description="Run every 1h",
            target_hours=1.0,
            warning_tolerance_hours=1.5,
            critical_tolerance_hours=2.0,
        ),
    ),
    backup_max_age_hours=24.0,
    scheduled_run_max_gap_hours=2.0,
    alert_dedup_minutes=60,
)


class SeverityForAgeTests(unittest.TestCase):
    """Table-driven tests for severity classification."""

    TABLE: list[tuple[float, float, float, str | None]] = [
        (0.5, 2.0, 3.0, None),
        (1.9, 2.0, 3.0, None),
        (2.1, 2.0, 3.0, "warning"),
        (2.5, 2.0, 3.0, "warning"),
        (3.1, 2.0, 3.0, "critical"),
        (10.0, 2.0, 3.0, "critical"),
        (0.0, 2.0, 3.0, None),
    ]

    def test_severity_classification(self) -> None:
        for age, warning, critical, expected in self.TABLE:
            with self.subTest(age=age, warning=warning, critical=critical):
                metric = FreshnessMetric(
                    name="test",
                    description="t",
                    target_hours=1.0,
                    warning_tolerance_hours=warning,
                    critical_tolerance_hours=critical,
                )
                result = _severity_for_age(age, metric)
                self.assertEqual(result, expected)


class CheckProductionForecastTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_fresh_forecast_is_healthy(self) -> None:
        state_path = self.root / "state.json"
        state_path.write_text(
            json.dumps({"forecasts": [{"latest_close_at": _iso(_utc_now())}]}),
            encoding="utf-8",
        )
        result = check_production_forecast(state_path, FORECAST_SLO_CONFIG, now=_utc_now())
        self.assertFalse(result.breached)
        self.assertIsNone(result.severity)

    def test_stale_forecast_is_critical(self) -> None:
        state_path = self.root / "state.json"
        stale_time = _iso(_utc_now() - timedelta(hours=5))
        state_path.write_text(
            json.dumps({"forecasts": [{"latest_close_at": stale_time}]}),
            encoding="utf-8",
        )
        result = check_production_forecast(state_path, FORECAST_SLO_CONFIG, now=_utc_now())
        self.assertTrue(result.breached)
        self.assertEqual(result.severity, BreachSeverity.critical)

    def test_missing_state_file_is_critical(self) -> None:
        result = check_production_forecast(
            self.root / "nonexistent.json", FORECAST_SLO_CONFIG, now=_utc_now()
        )
        self.assertTrue(result.breached)
        self.assertEqual(result.severity, BreachSeverity.critical)

    def test_empty_state_is_critical(self) -> None:
        state_path = self.root / "empty.json"
        state_path.write_text(json.dumps({}), encoding="utf-8")
        result = check_production_forecast(state_path, FORECAST_SLO_CONFIG, now=_utc_now())
        self.assertTrue(result.breached)
        self.assertEqual(result.severity, BreachSeverity.critical)


class CheckSiteUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_fresh_site_is_healthy(self) -> None:
        site_path = self.root / "data.json"
        site_path.write_text(
            json.dumps({"generated_at": _iso(_utc_now())}),
            encoding="utf-8",
        )
        result = check_site_update(site_path, FORECAST_SLO_CONFIG, now=_utc_now())
        self.assertFalse(result.breached)

    def test_stale_site_is_critical(self) -> None:
        site_path = self.root / "data.json"
        stale_time = _iso(_utc_now() - timedelta(hours=5))
        site_path.write_text(
            json.dumps({"generated_at": stale_time}),
            encoding="utf-8",
        )
        result = check_site_update(site_path, FORECAST_SLO_CONFIG, now=_utc_now())
        self.assertTrue(result.breached)
        self.assertEqual(result.severity, BreachSeverity.critical)

    def test_missing_site_file_is_critical(self) -> None:
        result = check_site_update(
            self.root / "nonexistent.json", FORECAST_SLO_CONFIG, now=_utc_now()
        )
        self.assertTrue(result.breached)
        self.assertEqual(result.severity, BreachSeverity.critical)

    def test_missing_generated_at_is_critical(self) -> None:
        site_path = self.root / "data.json"
        site_path.write_text(json.dumps({}), encoding="utf-8")
        result = check_site_update(site_path, FORECAST_SLO_CONFIG, now=_utc_now())
        self.assertTrue(result.breached)
        self.assertEqual(result.severity, BreachSeverity.critical)


class CheckHistoryBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_fresh_backup_is_healthy(self) -> None:
        assets_path = self.root / "assets.json"
        assets_path.write_text(
            json.dumps(
                [
                    {
                        "name": "forecast_history.backup-20260915.sqlite.gz",
                        "created_at": _iso(_utc_now() - timedelta(hours=2)),
                    },
                ]
            ),
            encoding="utf-8",
        )
        result = check_history_backup(assets_path, FORECAST_SLO_CONFIG, now=_utc_now())
        self.assertFalse(result.breached)

    def test_stale_backup_is_critical(self) -> None:
        assets_path = self.root / "assets.json"
        assets_path.write_text(
            json.dumps(
                [
                    {
                        "name": "forecast_history.backup-20260915.sqlite.gz",
                        "created_at": _iso(_utc_now() - timedelta(hours=30)),
                    },
                ]
            ),
            encoding="utf-8",
        )
        result = check_history_backup(assets_path, FORECAST_SLO_CONFIG, now=_utc_now())
        self.assertTrue(result.breached)
        self.assertEqual(result.severity, BreachSeverity.critical)

    def test_missing_assets_file_is_critical(self) -> None:
        result = check_history_backup(
            self.root / "nonexistent.json", FORECAST_SLO_CONFIG, now=_utc_now()
        )
        self.assertTrue(result.breached)
        self.assertEqual(result.severity, BreachSeverity.critical)

    def test_non_backup_assets_are_ignored(self) -> None:
        assets_path = self.root / "assets.json"
        assets_path.write_text(
            json.dumps(
                [
                    {
                        "name": "forecast_history.backup-old.sqlite.gz",
                        "created_at": _iso(_utc_now() - timedelta(hours=2)),
                    },
                    {
                        "name": "some-other-asset.zip",
                        "created_at": _iso(_utc_now() - timedelta(hours=1)),
                    },
                ]
            ),
            encoding="utf-8",
        )
        result = check_history_backup(assets_path, FORECAST_SLO_CONFIG, now=_utc_now())
        self.assertFalse(result.breached)


class CheckScheduledRunTests(unittest.TestCase):
    def test_fresh_run_is_healthy(self) -> None:
        runs = [
            {
                "event": "schedule",
                "status": "completed",
                "conclusion": "success",
                "updated_at": _iso(_utc_now() - timedelta(minutes=30)),
            },
        ]
        result = check_scheduled_run(runs, FORECAST_SLO_CONFIG, now=_utc_now())
        self.assertFalse(result.breached)

    def test_stale_run_is_critical(self) -> None:
        runs = [
            {
                "event": "schedule",
                "status": "completed",
                "conclusion": "success",
                "updated_at": _iso(_utc_now() - timedelta(hours=5)),
            },
        ]
        result = check_scheduled_run(runs, FORECAST_SLO_CONFIG, now=_utc_now())
        self.assertTrue(result.breached)
        self.assertEqual(result.severity, BreachSeverity.critical)

    def test_no_runs_is_critical(self) -> None:
        result = check_scheduled_run([], FORECAST_SLO_CONFIG, now=_utc_now())
        self.assertTrue(result.breached)
        self.assertEqual(result.severity, BreachSeverity.critical)

    def test_only_dispatch_runs_ignores_manual(self) -> None:
        runs = [
            {
                "event": "workflow_dispatch",
                "status": "completed",
                "conclusion": "success",
                "updated_at": _iso(_utc_now() - timedelta(hours=5)),
            },
        ]
        result = check_scheduled_run(runs, FORECAST_SLO_CONFIG, now=_utc_now())
        self.assertTrue(result.breached)

    def test_multiple_runs_picks_latest(self) -> None:
        runs = [
            {
                "event": "schedule",
                "status": "completed",
                "conclusion": "success",
                "updated_at": _iso(_utc_now() - timedelta(hours=10)),
            },
            {
                "event": "schedule",
                "status": "completed",
                "conclusion": "success",
                "updated_at": _iso(_utc_now() - timedelta(minutes=15)),
            },
        ]
        result = check_scheduled_run(runs, FORECAST_SLO_CONFIG, now=_utc_now())
        self.assertFalse(result.breached)


class DedupTests(unittest.TestCase):
    def test_should_alert_first_time(self) -> None:
        state = {"alerts": {}}
        self.assertTrue(_should_alert("key1", _utc_now(), 60, state))

    def test_should_not_alert_within_cooldown(self) -> None:
        state = {
            "alerts": {
                "key1": {
                    "last_alerted_at": _iso(_utc_now() - timedelta(minutes=10)),
                    "alert_count": 1,
                }
            }
        }
        self.assertFalse(_should_alert("key1", _utc_now(), 60, state))

    def test_should_alert_after_cooldown(self) -> None:
        state = {
            "alerts": {
                "key1": {
                    "last_alerted_at": _iso(_utc_now() - timedelta(minutes=70)),
                    "alert_count": 1,
                }
            }
        }
        self.assertTrue(_should_alert("key1", _utc_now(), 60, state))

    def test_should_alert_with_malformed_entry(self) -> None:
        state = {"alerts": {"key1": "bad"}}
        self.assertTrue(_should_alert("key1", _utc_now(), 60, state))

    def test_dedup_key_format(self) -> None:
        key = _dedup_key("production_forecast", "critical")
        self.assertEqual(key, "slo-breach:production_forecast:critical")


class RecoveryDetectionTests(unittest.TestCase):
    def test_recovery_detected_on_second_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state_path = root / "state.json"
            metrics_path = root / "events.jsonl"
            status_path = root / "status.json"

            stale_time = _iso(_utc_now() - timedelta(hours=5))
            state_path.write_text(
                json.dumps({"forecasts": [{"latest_close_at": stale_time}]}),
                encoding="utf-8",
            )

            summary1 = run_all_checks(
                FORECAST_SLO_CONFIG,
                history_db_path=state_path,
                now=_utc_now(),
                state_path=root / "watchdog.json",
                metrics_log_path=metrics_path,
                alert_status_path=status_path,
            )
            self.assertEqual(summary1["breaches"], 1)

            fresh_time = _iso(_utc_now() - timedelta(minutes=30))
            state_path.write_text(
                json.dumps({"forecasts": [{"latest_close_at": fresh_time}]}),
                encoding="utf-8",
            )

            summary2 = run_all_checks(
                FORECAST_SLO_CONFIG,
                history_db_path=state_path,
                now=_utc_now(),
                state_path=root / "watchdog.json",
                metrics_log_path=metrics_path,
                alert_status_path=status_path,
            )
            self.assertEqual(summary2["breaches"], 0)
            self.assertEqual(summary2["recoveries"], 1)


class AlertMessageFormattingTests(unittest.TestCase):
    def test_alert_message_includes_runbook(self) -> None:
        result = CheckResult(
            metric_name="production_forecast",
            severity="critical",
            measured_hours=5.0,
            target_hours=1.0,
            message="stale forecast",
            breached=True,
        )
        msg = _format_alert_message(result, "critical")
        self.assertIn("CRITICAL", msg)
        self.assertIn("production_forecast", msg)
        self.assertIn("FRESHNESS_SLO.md", msg)

    def test_recovery_message(self) -> None:
        result = CheckResult(
            metric_name="production_forecast",
            severity=None,
            measured_hours=0.5,
            target_hours=1.0,
            message="fresh forecast",
            breached=False,
        )
        msg = _format_recovery_message(result)
        self.assertIn("RECOVERED", msg)
        self.assertIn("production_forecast", msg)


class RunAllChecksIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_all_healthy(self) -> None:
        state_path = self.root / "state.json"
        state_path.write_text(
            json.dumps({"forecasts": [{"latest_close_at": _iso(_utc_now())}]}),
            encoding="utf-8",
        )
        site_path = self.root / "site.json"
        site_path.write_text(
            json.dumps({"generated_at": _iso(_utc_now())}),
            encoding="utf-8",
        )
        assets_path = self.root / "assets.json"
        assets_path.write_text(
            json.dumps(
                [
                    {
                        "name": "forecast_history.backup-20260915.sqlite.gz",
                        "created_at": _iso(_utc_now() - timedelta(hours=2)),
                    },
                ]
            ),
            encoding="utf-8",
        )
        runs = [
            {
                "event": "schedule",
                "status": "completed",
                "conclusion": "success",
                "updated_at": _iso(_utc_now() - timedelta(minutes=20)),
            },
        ]
        summary = run_all_checks(
            FORECAST_SLO_CONFIG,
            history_db_path=state_path,
            site_data_path=site_path,
            backup_assets_path=assets_path,
            workflow_runs=runs,
            now=_utc_now(),
            state_path=self.root / "watchdog.json",
            metrics_log_path=self.root / "events.jsonl",
            alert_status_path=self.root / "status.json",
        )
        self.assertEqual(summary["breaches"], 0)
        self.assertEqual(summary["alerts_emitted"], 0)

    def test_all_breached(self) -> None:
        stale = _iso(_utc_now() - timedelta(hours=50))
        state_path = self.root / "state.json"
        state_path.write_text(
            json.dumps({"forecasts": [{"latest_close_at": stale}]}),
            encoding="utf-8",
        )
        site_path = self.root / "site.json"
        site_path.write_text(
            json.dumps({"generated_at": stale}),
            encoding="utf-8",
        )
        assets_path = self.root / "assets.json"
        assets_path.write_text(
            json.dumps(
                [
                    {"name": "forecast_history.backup-20260915.sqlite.gz", "created_at": stale},
                ]
            ),
            encoding="utf-8",
        )
        runs = [
            {
                "event": "schedule",
                "status": "completed",
                "conclusion": "success",
                "updated_at": stale,
            },
        ]
        summary = run_all_checks(
            FORECAST_SLO_CONFIG,
            history_db_path=state_path,
            site_data_path=site_path,
            backup_assets_path=assets_path,
            workflow_runs=runs,
            now=_utc_now(),
            state_path=self.root / "watchdog.json",
            metrics_log_path=self.root / "events.jsonl",
            alert_status_path=self.root / "status.json",
        )
        self.assertEqual(summary["breaches"], 4)
        self.assertGreaterEqual(summary["alerts_emitted"], 1)

    def test_no_false_negative_on_critical_breach(self) -> None:
        """Critical breach must always be detected — no false negatives."""
        stale = _iso(_utc_now() - timedelta(hours=10))
        state_path = self.root / "state.json"
        state_path.write_text(
            json.dumps({"forecasts": [{"latest_close_at": stale}]}),
            encoding="utf-8",
        )
        result = check_production_forecast(state_path, FORECAST_SLO_CONFIG, now=_utc_now())
        self.assertTrue(result.breached, "Critical breach was missed (false negative)")
        self.assertEqual(result.severity, BreachSeverity.critical)

    def test_dedup_suppresses_duplicate_alerts(self) -> None:
        stale = _iso(_utc_now() - timedelta(hours=5))
        state_path = self.root / "state.json"
        state_path.write_text(
            json.dumps({"forecasts": [{"latest_close_at": stale}]}),
            encoding="utf-8",
        )
        s1 = run_all_checks(
            FORECAST_SLO_CONFIG,
            history_db_path=state_path,
            now=_utc_now(),
            state_path=self.root / "watchdog.json",
            metrics_log_path=self.root / "events.jsonl",
            alert_status_path=self.root / "status.json",
        )
        s2 = run_all_checks(
            FORECAST_SLO_CONFIG,
            history_db_path=state_path,
            now=_utc_now() + timedelta(minutes=5),
            state_path=self.root / "watchdog.json",
            metrics_log_path=self.root / "events.jsonl",
            alert_status_path=self.root / "status.json",
        )
        self.assertGreaterEqual(s1["alerts_emitted"], 1)
        self.assertEqual(s2["alerts_emitted"], 0)


class HelperFunctionTests(unittest.TestCase):
    def test_latest_history_origin_from_forecasts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text(
                json.dumps(
                    {
                        "forecasts": [
                            {"latest_close_at": "2026-09-15T10:00:00+00:00"},
                            {"latest_close_at": "2026-09-15T14:00:00+00:00"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            result = _latest_history_origin(path)
            self.assertEqual(result.hour, 14)  # type: ignore[union-attr]

    def test_latest_history_origin_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "empty.json"
            path.write_text(json.dumps({}), encoding="utf-8")
            self.assertIsNone(_latest_history_origin(path))

    def test_latest_backup_time(self) -> None:
        assets = [
            {
                "name": "forecast_history.backup-old.sqlite.gz",
                "created_at": "2026-09-15T08:00:00+00:00",
            },
            {
                "name": "forecast_history.backup-new.sqlite.gz",
                "created_at": "2026-09-15T20:00:00+00:00",
            },
            {"name": "unrelated.zip"},
        ]
        result = _latest_backup_time(assets)
        self.assertIsNotNone(result)
        self.assertEqual(result.hour, 20)  # type: ignore[union-attr]

    def test_latest_backup_time_empty(self) -> None:
        self.assertIsNone(_latest_backup_time([]))

    def test_latest_scheduled_run(self) -> None:
        runs = [
            {"event": "schedule", "updated_at": "2026-09-15T08:00:00+00:00"},
            {"event": "workflow_dispatch", "updated_at": "2026-09-15T12:00:00+00:00"},
            {"event": "schedule", "updated_at": "2026-09-15T14:00:00+00:00"},
        ]
        result = _latest_scheduled_run(runs)
        self.assertIsNotNone(result)
        self.assertEqual(result["updated_at"], "2026-09-15T14:00:00+00:00")

    def test_latest_scheduled_run_none(self) -> None:
        self.assertIsNone(_latest_scheduled_run([]))

    def test_load_state_creates_empty_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state = _load_state(Path(tmpdir) / "missing.json")
            self.assertEqual(state["version"], 1)
            self.assertEqual(state["alerts"], {})

    def test_save_and_load_state_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            state = {
                "version": 1,
                "alerts": {
                    "key1": {"last_alerted_at": "2026-09-15T10:00:00+00:00", "alert_count": 2}
                },
                "last_run_at": "2026-09-15T10:00:00+00:00",
                "breaches": [],
                "recoveries": [],
            }
            _save_state(state, state_path)
            loaded = _load_state(state_path)
            self.assertEqual(loaded["alerts"]["key1"]["alert_count"], 2)


class WeeklyAdherenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_events(self, path: Path, events: list[dict[str, object]]) -> None:
        lines = "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n"
        path.write_text(lines, encoding="utf-8")

    def test_empty_log_returns_zero_checks(self) -> None:
        result = compute_weekly_slo_adherence(self.root / "missing.jsonl", now=_utc_now())
        self.assertEqual(result["overall"]["checks"], 0)
        self.assertEqual(result["overall"]["adherence_pct"], 100.0)
        self.assertEqual(result["per_metric"], {})

    def test_mixed_events_compute_adherence(self) -> None:
        log_path = self.root / "events.jsonl"
        now = _utc_now()
        events = []
        for i in range(8):
            events.append(
                {
                    "timestamp": _iso(now - timedelta(minutes=30 * i)),
                    "event": "check_ok",
                    "metric_name": "production_forecast",
                }
            )
        for i in range(2):
            events.append(
                {
                    "timestamp": _iso(now - timedelta(minutes=35 * i)),
                    "event": "breach",
                    "metric_name": "production_forecast",
                }
            )
        events.append(
            {
                "timestamp": _iso(now - timedelta(minutes=10)),
                "event": "check_ok",
                "metric_name": "site_update",
            }
        )
        self._write_events(log_path, events)
        result = compute_weekly_slo_adherence(log_path, now=now)
        self.assertEqual(result["window_days"], 7)
        self.assertEqual(result["overall"]["checks"], 11)
        self.assertEqual(result["overall"]["breaches"], 2)
        metric = result["per_metric"]["production_forecast"]
        self.assertEqual(metric["checks"], 10)
        self.assertEqual(metric["breaches"], 2)
        self.assertEqual(metric["adherence_pct"], 80.0)

    def test_old_events_outside_window_are_excluded(self) -> None:
        log_path = self.root / "events.jsonl"
        now = _utc_now()
        self._write_events(
            log_path,
            [
                {
                    "timestamp": _iso(now - timedelta(days=8)),
                    "event": "breach",
                    "metric_name": "production_forecast",
                },
                {
                    "timestamp": _iso(now - timedelta(days=8)),
                    "event": "check_ok",
                    "metric_name": "site_update",
                },
            ],
        )
        result = compute_weekly_slo_adherence(log_path, now=now, days=7)
        self.assertEqual(result["overall"]["checks"], 0)
        self.assertEqual(result["per_metric"], {})

    def test_ignores_unrelated_event_types(self) -> None:
        log_path = self.root / "events.jsonl"
        now = _utc_now()
        self._write_events(
            log_path,
            [
                {
                    "timestamp": _iso(now - timedelta(minutes=5)),
                    "event": "recovery",
                    "metric_name": "production_forecast",
                },
                {
                    "timestamp": _iso(now - timedelta(minutes=5)),
                    "event": "check_ok",
                    "metric_name": "production_forecast",
                },
            ],
        )
        result = compute_weekly_slo_adherence(log_path, now=now)
        self.assertEqual(result["overall"]["checks"], 1)
        self.assertEqual(result["overall"]["breaches"], 0)

    def test_invalid_days_raises(self) -> None:
        with self.assertRaises(ValueError):
            compute_weekly_slo_adherence(self.root / "e.jsonl", days=0)


class NoFalseNegativeTests(unittest.TestCase):
    """Every breach scenario must be detected — no false negatives allowed."""

    TABLE: list[tuple[str, str]] = [
        ("stale_forecast_5h", "production_forecast"),
        ("stale_site_5h", "site_update"),
        ("stale_backup_30h", "history_backup"),
        ("missed_scheduled_run_5h", "scheduled_run"),
    ]

    def test_no_false_negative(self) -> None:
        stale = _iso(_utc_now() - timedelta(hours=50))
        for scenario, expected_metric in self.TABLE:
            with self.subTest(scenario=scenario):
                if expected_metric == "production_forecast":
                    with tempfile.TemporaryDirectory() as tmpdir:
                        state_path = Path(tmpdir) / "state.json"
                        state_path.write_text(
                            json.dumps({"forecasts": [{"latest_close_at": stale}]}),
                            encoding="utf-8",
                        )
                        result = check_production_forecast(
                            state_path, FORECAST_SLO_CONFIG, now=_utc_now()
                        )
                        self.assertTrue(result.breached, f"False negative for {scenario}")

                elif expected_metric == "site_update":
                    with tempfile.TemporaryDirectory() as tmpdir:
                        site_path = Path(tmpdir) / "data.json"
                        site_path.write_text(
                            json.dumps({"generated_at": stale}),
                            encoding="utf-8",
                        )
                        result = check_site_update(site_path, FORECAST_SLO_CONFIG, now=_utc_now())
                        self.assertTrue(result.breached, f"False negative for {scenario}")

                elif expected_metric == "history_backup":
                    with tempfile.TemporaryDirectory() as tmpdir:
                        assets_path = Path(tmpdir) / "assets.json"
                        assets_path.write_text(
                            json.dumps(
                                [
                                    {
                                        "name": "forecast_history.backup-old.sqlite.gz",
                                        "created_at": stale,
                                    },
                                ]
                            ),
                            encoding="utf-8",
                        )
                        result = check_history_backup(
                            assets_path, FORECAST_SLO_CONFIG, now=_utc_now()
                        )
                        self.assertTrue(result.breached, f"False negative for {scenario}")

                elif expected_metric == "scheduled_run":
                    runs = [
                        {
                            "event": "schedule",
                            "status": "completed",
                            "conclusion": "success",
                            "updated_at": stale,
                        },
                    ]
                    result = check_scheduled_run(runs, FORECAST_SLO_CONFIG, now=_utc_now())
                    self.assertTrue(result.breached, f"False negative for {scenario}")


if __name__ == "__main__":
    unittest.main()
