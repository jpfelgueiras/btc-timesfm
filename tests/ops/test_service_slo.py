#!/usr/bin/env python3
"""Tests for forecast-service SLO evaluation and error-budget accounting."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from btc_timesfm.ops.service_slo import (
    ServiceSLO,
    ServiceSLOConfig,
    build_dashboard,
    dashboard_markdown,
    evaluate_objective,
    load_config,
    load_events,
)


UTC = timezone.utc
NOW = datetime(2026, 9, 16, tzinfo=UTC)


class ServiceSLOTests(unittest.TestCase):
    def objective(self, **changes: object) -> ServiceSLO:
        values: dict[str, object] = {
            "name": "model-execution",
            "domain": "model",
            "description": "Forecast inference.",
            "target_percent": 90.0,
            "window_days": 30,
            "min_events": 1,
            "review_cadence_days": 90,
            "threshold_basis": "One normalized inference outcome per run.",
        }
        values.update(changes)
        return ServiceSLO(
            name=str(values["name"]),
            domain=str(values["domain"]),
            description=str(values["description"]),
            target_percent=float(values["target_percent"]),
            window_days=int(values["window_days"]),
            min_events=int(values["min_events"]),
            review_cadence_days=int(values["review_cadence_days"]),
            threshold_basis=str(values["threshold_basis"]),
        )

    def test_evaluation_accounts_for_remaining_error_budget(self) -> None:
        objective = self.objective()
        events = [
            {"domain": "model", "status": "success", "timestamp": NOW.isoformat()},
            {"domain": "model", "status": "success", "timestamp": NOW.isoformat()},
            {"domain": "model", "status": "success", "timestamp": NOW.isoformat()},
            {"domain": "model", "status": "success", "timestamp": NOW.isoformat()},
            {"domain": "model", "status": "failed", "timestamp": NOW.isoformat()},
        ]
        result = evaluate_objective(objective, events, now=NOW)
        self.assertEqual(result["status"], "breached")
        self.assertEqual(result["events"], 5)
        self.assertEqual(result["failures"], 1)
        self.assertEqual(result["allowed_failures"], 0.5)
        self.assertEqual(result["remaining_error_budget"], -0.5)
        self.assertEqual(result["error_budget_consumed_percent"], 200.0)

    def test_evaluation_excludes_old_and_unrecognized_events(self) -> None:
        objective = self.objective(min_events=2)
        events = [
            {"domain": "model", "status": "success", "timestamp": NOW.isoformat()},
            {"domain": "model", "status": "running", "timestamp": NOW.isoformat()},
            {
                "domain": "model",
                "status": "failed",
                "timestamp": (NOW - timedelta(days=31)).isoformat(),
            },
        ]
        result = evaluate_objective(objective, events, now=NOW)
        self.assertEqual(result["events"], 1)
        self.assertEqual(result["status"], "insufficient_data")

    def test_dashboard_distinguishes_every_required_domain(self) -> None:
        domains = ("data", "model", "storage", "api", "publication")
        config = ServiceSLOConfig(
            schema_version=1,
            service="forecast-service",
            objectives=tuple(self.objective(name=domain, domain=domain) for domain in domains),
        )
        dashboard = build_dashboard(config, [], now=NOW)
        self.assertEqual(tuple(dashboard["domains"]), domains)
        self.assertIn("Forecast service reliability dashboard", dashboard_markdown(dashboard))

    def test_config_requires_all_domains_and_valid_version(self) -> None:
        with self.assertRaisesRegex(ValueError, "cover"):
            ServiceSLOConfig(
                schema_version=1, service="forecast-service", objectives=(self.objective(),)
            )
        with self.assertRaisesRegex(ValueError, "version"):
            ServiceSLOConfig(schema_version=2, service="forecast-service", objectives=())

    def test_load_config_and_events(self) -> None:
        source = Path(__file__).parents[2] / "service_slo.json"
        config = load_config(source)
        self.assertEqual(config.schema_version, 1)
        self.assertEqual(
            {item.domain for item in config.objectives},
            {"data", "model", "storage", "api", "publication"},
        )
        with tempfile.TemporaryDirectory() as directory:
            events_path = Path(directory) / "events.jsonl"
            events_path.write_text(
                json.dumps({"domain": "data", "status": "success"}) + "\ninvalid\n",
                encoding="utf-8",
            )
            self.assertEqual(len(load_events(events_path)), 1)


if __name__ == "__main__":
    unittest.main()
