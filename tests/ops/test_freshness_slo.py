#!/usr/bin/env python3
"""Tests for freshness SLO configuration module."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timezone
from pathlib import Path

from btc_timesfm.ops.freshness_slo import (
    FreshnessMetric,
    FreshnessSLOConfig,
    default_slo_config,
    load_config,
    save_config,
)


UTC = timezone.utc


class FreshnessMetricTests(unittest.TestCase):
    def test_valid_metric_creation(self) -> None:
        metric = FreshnessMetric(
            name="test",
            description="desc",
            target_hours=1.0,
            warning_tolerance_hours=2.0,
            critical_tolerance_hours=3.0,
        )
        self.assertEqual(metric.name, "test")
        self.assertEqual(metric.target_hours, 1.0)

    def test_target_hours_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            FreshnessMetric(
                name="bad",
                description="desc",
                target_hours=0,
                warning_tolerance_hours=1.0,
                critical_tolerance_hours=2.0,
            )

    def test_warning_tolerance_must_be_non_negative(self) -> None:
        with self.assertRaises(ValueError):
            FreshnessMetric(
                name="bad",
                description="desc",
                target_hours=1.0,
                warning_tolerance_hours=-1.0,
                critical_tolerance_hours=2.0,
            )

    def test_critical_must_be_at_least_warning(self) -> None:
        with self.assertRaises(ValueError):
            FreshnessMetric(
                name="bad",
                description="desc",
                target_hours=1.0,
                warning_tolerance_hours=3.0,
                critical_tolerance_hours=2.0,
            )

    def test_as_dict_returns_all_fields(self) -> None:
        metric = FreshnessMetric(
            name="prod",
            description="production forecast",
            target_hours=1.0,
            warning_tolerance_hours=2.0,
            critical_tolerance_hours=3.0,
        )
        d = metric.as_dict()
        self.assertEqual(d["name"], "prod")
        self.assertEqual(d["target_hours"], 1.0)
        self.assertIn("warning_tolerance_hours", d)
        self.assertIn("critical_tolerance_hours", d)


class FreshnessSLOConfigTests(unittest.TestCase):
    def test_default_config_has_four_metrics(self) -> None:
        config = default_slo_config()
        self.assertEqual(len(config.metrics), 4)
        self.assertEqual(config.schema_version, 1)

    def test_metric_by_name_returns_correct_metric(self) -> None:
        config = default_slo_config()
        metric = config.metric_by_name("production_forecast")
        self.assertIsNotNone(metric)
        self.assertEqual(metric.name, "production_forecast")  # type: ignore[union-attr]
        self.assertIsNone(config.metric_by_name("nonexistent"))

    def test_all_metric_names(self) -> None:
        config = default_slo_config()
        names = config.all_metric_names()
        self.assertEqual(len(names), 4)
        self.assertIn("production_forecast", names)
        self.assertIn("site_update", names)
        self.assertIn("history_backup", names)
        self.assertIn("scheduled_run", names)

    def test_as_dict_round_trip(self) -> None:
        config = default_slo_config()
        d = config.as_dict()
        self.assertEqual(d["schema_version"], 1)
        self.assertEqual(len(d["metrics"]), 4)
        self.assertIn("backup_max_age_hours", d)
        self.assertIn("alert_dedup_minutes", d)

    def test_invalid_schema_version_raises(self) -> None:
        with self.assertRaises(ValueError):
            FreshnessSLOConfig(schema_version=999)

    def test_invalid_backup_max_age_raises(self) -> None:
        with self.assertRaises(ValueError):
            FreshnessSLOConfig(backup_max_age_hours=0)

    def test_invalid_scheduled_run_gap_raises(self) -> None:
        with self.assertRaises(ValueError):
            FreshnessSLOConfig(scheduled_run_max_gap_hours=-1)

    def test_invalid_alert_dedup_raises(self) -> None:
        with self.assertRaises(ValueError):
            FreshnessSLOConfig(alert_dedup_minutes=0)


class LoadSaveConfigTests(unittest.TestCase):
    def test_load_returns_defaults_when_file_missing(self) -> None:
        config = load_config("/nonexistent/path.json")
        self.assertEqual(config.schema_version, 1)
        self.assertEqual(len(config.metrics), 4)

    def test_load_returns_defaults_when_file_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bad.json"
            path.write_text("not json", encoding="utf-8")
            config = load_config(path)
            self.assertEqual(len(config.metrics), 4)

    def test_load_returns_defaults_when_file_not_dict(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "list.json"
            path.write_text("[]", encoding="utf-8")
            config = load_config(path)
            self.assertEqual(len(config.metrics), 4)

    def test_load_returns_defaults_on_wrong_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "old.json"
            path.write_text(
                json.dumps({"schema_version": 99, "metrics": []}),
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual(len(config.metrics), 4)

    def test_save_and_load_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "slo.json"
            original = default_slo_config()
            save_config(original, path)
            loaded = load_config(path)
            self.assertEqual(loaded.schema_version, original.schema_version)
            self.assertEqual(len(loaded.metrics), len(original.metrics))
            self.assertEqual(loaded.backup_max_age_hours, original.backup_max_age_hours)

    def test_load_skips_malformed_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "partial.json"
            payload = {
                "schema_version": 1,
                "metrics": [
                    {
                        "name": "valid",
                        "description": "ok",
                        "target_hours": 1.0,
                        "warning_tolerance_hours": 2.0,
                        "critical_tolerance_hours": 3.0,
                    },
                    {"name": "bad", "description": "missing fields"},
                ],
                "backup_max_age_hours": 24.0,
                "scheduled_run_max_gap_hours": 2.0,
                "alert_dedup_minutes": 60,
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            config = load_config(path)
            self.assertEqual(len(config.metrics), 1)
            self.assertEqual(config.metrics[0].name, "valid")


class DefaultSLOConfigThresholdsTests(unittest.TestCase):
    """Table-driven tests for SLO threshold relationships."""

    TABLE: list[tuple[str, float, float, float]] = [
        ("production_forecast", 1.0, 2.0, 3.0),
        ("site_update", 1.0, 1.5, 2.0),
        ("history_backup", 12.0, 20.0, 24.0),
        ("scheduled_run", 1.0, 1.5, 2.0),
    ]

    def test_targets_and_thresholds(self) -> None:
        config = default_slo_config()
        for name, target, warning, critical in self.TABLE:
            with self.subTest(metric=name):
                metric = config.metric_by_name(name)
                self.assertIsNotNone(metric, f"metric {name} not found")
                self.assertEqual(metric.target_hours, target)  # type: ignore[union-attr]
                self.assertEqual(metric.warning_tolerance_hours, warning)  # type: ignore[union-attr]
                self.assertEqual(metric.critical_tolerance_hours, critical)  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
