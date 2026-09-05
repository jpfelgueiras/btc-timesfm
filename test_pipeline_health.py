#!/usr/bin/env python3
"""Tests for durable pipeline health, publication gates and circuit recovery."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from pipeline_health import HealthConfig, PipelineHealth, notify_webhook


UTC = timezone.utc


class PipelineHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_path = self.root / "pipeline_health.json"
        self.validation_path = self.root / "validation.json"
        self.drift_path = self.root / "drift.json"
        self.x_status_path = self.root / "x_status.json"
        self.health = PipelineHealth(
            self.state_path,
            config=HealthConfig(x_post_cooldown_minutes=60),
        )
        self.now = datetime(2026, 9, 5, 20, 0, tzinfo=UTC)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_transient_failure_degrades_then_threshold_opens_circuit(self) -> None:
        self.health.record_failure(
            "market_data", failure_class="validation", detail="stale", now=self.now
        )
        stage = self.health.state["stages"]["market_data"]
        self.assertEqual(stage["health"], "degraded")
        self.assertEqual(stage["circuit_state"], "closed")
        self.assertEqual(stage["consecutive_failures"], 1)

        self.health.record_failure(
            "market_data",
            failure_class="validation",
            detail="stale",
            now=self.now + timedelta(hours=1),
        )
        stage = self.health.state["stages"]["market_data"]
        self.assertEqual(stage["health"], "open")
        self.assertEqual(stage["circuit_state"], "open")
        self.assertEqual(stage["consecutive_failures"], 2)

    def test_success_resets_failure_count_and_closes_circuit(self) -> None:
        self.health.record_failure("forecast", failure_class="execution", now=self.now)
        self.health.record_failure(
            "forecast", failure_class="execution", now=self.now + timedelta(minutes=1)
        )
        self.assertEqual(self.health.state["stages"]["forecast"]["circuit_state"], "open")

        self.health.record_success("forecast", now=self.now + timedelta(minutes=2))
        stage = self.health.state["stages"]["forecast"]
        self.assertEqual(stage["circuit_state"], "closed")
        self.assertEqual(stage["health"], "healthy")
        self.assertEqual(stage["consecutive_failures"], 0)

    def test_x_circuit_blocks_until_cooldown_then_allows_one_half_open_probe(self) -> None:
        for minute in range(3):
            self.health.record_failure(
                "x_post",
                failure_class="authentication",
                now=self.now + timedelta(minutes=minute),
            )
        opened = self.health.state["stages"]["x_post"]["opened_at"]
        self.assertIsNotNone(opened)

        blocked = self.health.publication_gate(now=self.now + timedelta(minutes=30))
        self.assertFalse(blocked["publication_allowed"])
        self.assertIn("x_post_circuit_open", blocked["blockers"])

        probe = self.health.publication_gate(now=self.now + timedelta(minutes=62))
        self.assertTrue(probe["publication_allowed"])
        self.assertTrue(probe["x_half_open_probe"])
        self.assertEqual(self.health.state["stages"]["x_post"]["circuit_state"], "half_open")

        second = self.health.publication_gate(now=self.now + timedelta(minutes=63))
        self.assertFalse(second["publication_allowed"])
        self.assertIn("x_post_half_open_probe_in_progress", second["blockers"])

    def test_half_open_success_recovers_and_failure_reopens(self) -> None:
        for minute in range(3):
            self.health.record_failure(
                "x_post", failure_class="rate_limit", now=self.now + timedelta(minutes=minute)
            )
        self.health.publication_gate(now=self.now + timedelta(minutes=62))
        self.health.record_success("x_post", now=self.now + timedelta(minutes=63))
        self.assertEqual(self.health.state["stages"]["x_post"]["circuit_state"], "closed")
        self.assertEqual(self.health.state["stages"]["x_post"]["consecutive_failures"], 0)

        for minute in range(3):
            self.health.record_failure(
                "x_post",
                failure_class="provider_error",
                now=self.now + timedelta(hours=2, minutes=minute),
            )
        self.health.publication_gate(now=self.now + timedelta(hours=3, minutes=2))
        self.health.record_failure(
            "x_post",
            failure_class="provider_error",
            now=self.now + timedelta(hours=3, minutes=3),
        )
        self.assertEqual(self.health.state["stages"]["x_post"]["circuit_state"], "open")
        self.assertEqual(
            self.health.state["stages"]["x_post"]["opened_at"], "2026-09-05T23:03:00+00:00"
        )

    def test_severe_drift_blocks_publication_even_with_healthy_forecast(self) -> None:
        self.validation_path.write_text(json.dumps({"status": "ok"}), encoding="utf-8")
        self.drift_path.write_text(json.dumps({"severity": "severe"}), encoding="utf-8")
        self.health.observe_forecast(
            outcome="success",
            validation_path=self.validation_path,
            drift_path=self.drift_path,
            now=self.now,
        )
        self.health.observe_history(outcome="success", now=self.now)
        report = self.health.publication_gate(now=self.now)
        self.assertFalse(report["publication_allowed"])
        self.assertIn("severe_model_or_feature_drift", report["blockers"])

    def test_current_validation_failure_blocks_before_circuit_threshold(self) -> None:
        self.validation_path.write_text(
            json.dumps({"status": "failed", "errors": [{"code": "stale_data"}]}),
            encoding="utf-8",
        )
        self.drift_path.write_text(json.dumps({"severity": "none"}), encoding="utf-8")
        self.health.observe_forecast(
            outcome="failure",
            validation_path=self.validation_path,
            drift_path=self.drift_path,
            now=self.now,
        )
        report = self.health.publication_gate(now=self.now, ignore_stages=("history",))
        self.assertFalse(report["publication_allowed"])
        self.assertIn("current_market_data_unhealthy", report["blockers"])
        self.assertEqual(self.health.state["stages"]["market_data"]["consecutive_failures"], 1)

    def test_history_failure_blocks_current_publication_and_success_recovers(self) -> None:
        self.health.state["current_signals"]["market_data_status"] = "healthy"
        self.health.state["current_signals"]["forecast_outcome"] = "success"
        self.health.state["current_signals"]["drift_severity"] = "none"
        self.health.observe_history(outcome="failure", now=self.now)
        blocked = self.health.publication_gate(now=self.now)
        self.assertFalse(blocked["publication_allowed"])
        self.assertIn("current_history_persistence_failed", blocked["blockers"])

        self.health.observe_history(outcome="success", now=self.now + timedelta(minutes=1))
        recovered = self.health.publication_gate(now=self.now + timedelta(minutes=1))
        self.assertTrue(recovered["publication_allowed"])

    def test_x_status_distinguishes_failure_and_success_recovery(self) -> None:
        self.x_status_path.write_text(
            json.dumps({"status": "preflight_failed", "failure_class": "authentication"}),
            encoding="utf-8",
        )
        self.health.observe_x_status(
            status_path=self.x_status_path, phase="preflight", now=self.now
        )
        self.assertEqual(self.health.state["stages"]["x_post"]["consecutive_failures"], 1)

        self.x_status_path.write_text(json.dumps({"status": "posted"}), encoding="utf-8")
        self.health.observe_x_status(
            status_path=self.x_status_path,
            phase="publish",
            now=self.now + timedelta(minutes=1),
        )
        self.assertEqual(self.health.state["stages"]["x_post"]["consecutive_failures"], 0)
        self.assertEqual(self.health.state["stages"]["x_post"]["health"], "healthy")

    def test_state_round_trips_and_events_are_bounded(self) -> None:
        for minute in range(105):
            self.health.record_success("forecast", now=self.now + timedelta(minutes=minute))
        reloaded = PipelineHealth(self.state_path, config=HealthConfig(x_post_cooldown_minutes=60))
        self.assertLessEqual(len(reloaded.state["events"]), 100)
        self.assertEqual(reloaded.state["stages"]["forecast"]["health"], "healthy")

    def test_notify_is_noop_without_webhook(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(
                notify_webhook(
                    {
                        "overall_health": "open",
                        "publication_allowed": False,
                        "blockers": ["forecast_circuit_open"],
                    }
                )
            )


if __name__ == "__main__":
    unittest.main()
