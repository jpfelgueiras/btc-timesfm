#!/usr/bin/env python3
"""Tests for the workflow guardian watchdog module."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta, timezone
from pathlib import Path

from btc_timesfm.ops.freshness_slo import _iso, _utc_now, default_slo_config
from btc_timesfm.ops.workflow_guardian import (
    GuardianActions,
    GuardianConfig,
    Recovery,
    check_missed_schedule,
    classify_failure,
    decide_escalation,
    decide_retry,
    default_workflows,
    incident_key,
    recover_incidents,
    run_guard,
)
from btc_timesfm.ops.workflow_guardian import _fresh_state, _latest_run

UTC = timezone.utc

NOW = _utc_now()

SPECS = {spec.name: spec for spec in default_workflows()}


def _run(
    *,
    run_id: int = 1,
    conclusion: str = "success",
    status: str = "completed",
    started: str | None = None,
    error_text: str = "",
    event: str = "schedule",
) -> dict[str, object]:
    start = started or _iso(NOW - timedelta(minutes=10))
    return {
        "id": run_id,
        "status": status,
        "conclusion": conclusion,
        "created_at": start,
        "run_started_at": start,
        "event": event,
        "error_text": error_text,
    }


class ClassificationTests(unittest.TestCase):
    """Table-driven tests for transient vs persistent failure classification."""

    TABLE: list[tuple[str, str, str, str, bool]] = [
        (
            "rate_limit_marker",
            "API rate limit exceeded for user jpfelgueiras",
            "transient",
            "rate_limit",
            True,
        ),
        (
            "quiesced_runner",
            "The runner has been terminated while running this job. The runner is shutting down.",
            "transient",
            "runner_quiesced",
            True,
        ),
        (
            "network_unreachable",
            "Could not resolve host: pypi.org (Temporary failure in name resolution)",
            "transient",
            "network",
            True,
        ),
        (
            "upstream_503",
            "HTTPError: 503 Service Unavailable received from upstream API",
            "transient",
            "upstream_api",
            True,
        ),
        (
            "validation_failure",
            "market data validation failed: close prices missing",
            "persistent",
            "validation",
            False,
        ),
        (
            "code_error",
            "Traceback (most recent call last): KeyError: 'latest_close_at'",
            "persistent",
            "code_error",
            False,
        ),
        (
            "unknown",
            "something entirely unexpected happened",
            "persistent",
            "unknown",
            False,
        ),
    ]

    def test_table(self) -> None:
        for scenario, error_text, kind, category, retryable in self.TABLE:
            with self.subTest(scenario=scenario):
                result = classify_failure(
                    workflow="forecast",
                    conclusion="failure",
                    status="completed",
                    error_text=error_text,
                )
                self.assertEqual(result.kind, kind, scenario)
                self.assertEqual(result.category, category, scenario)
                self.assertEqual(result.retryable, retryable, scenario)

    def test_empty_input_is_persistent_unknown(self) -> None:
        result = classify_failure(workflow="forecast", conclusion="failure")
        self.assertEqual(result.kind, "persistent")
        self.assertEqual(result.category, "unknown")


class RetryDecisionTests(unittest.TestCase):
    """Table-driven tests for the retry-once-with-backoff policy."""

    CONFIG = GuardianConfig(retry_attempts=1, retry_backoff_minutes=15)

    def _transient(self, *, started: str | None = None) -> dict[str, object]:
        return _run(
            run_id=1,
            conclusion="failure",
            status="completed",
            started=started,
            error_text="API rate limit exceeded",
        )

    def test_persistent_failure_is_not_retryable(self) -> None:
        run = _run(run_id=1, conclusion="failure", error_text="validation failed")
        state = _fresh_state()
        decision = decide_retry(
            SPECS["forecast"],
            run,
            classify_failure(workflow="forecast", error_text="validation failed"),
            config=self.CONFIG,
            state=state,
            now=NOW,
        )
        self.assertEqual(decision.action, "not_retryable")
        self.assertFalse(decision.dispatched)

    def test_transient_before_backoff_is_deferred(self) -> None:
        started = _iso(NOW - timedelta(minutes=5))
        run = self._transient(started=started)
        decision = decide_retry(
            SPECS["forecast"],
            run,
            classify_failure(workflow="forecast", error_text="rate limit"),
            config=self.CONFIG,
            state=_fresh_state(),
            now=NOW,
        )
        self.assertEqual(decision.action, "deferred")
        self.assertFalse(decision.dispatched)

    def test_transient_after_backoff_is_dispatched(self) -> None:
        started = _iso(NOW - timedelta(minutes=30))
        run = self._transient(started=started)
        state = _fresh_state()
        decision = decide_retry(
            SPECS["forecast"],
            run,
            classify_failure(workflow="forecast", error_text="rate limit"),
            config=self.CONFIG,
            state=state,
            now=NOW,
        )
        self.assertEqual(decision.action, "dispatch")
        self.assertFalse(decision.dispatched)

    def test_retry_budget_is_consumed_once(self) -> None:
        started = _iso(NOW - timedelta(minutes=30))
        run = self._transient(started=started)
        state = _fresh_state()
        decision = decide_retry(
            SPECS["forecast"],
            run,
            classify_failure(workflow="forecast", error_text="rate limit"),
            config=self.CONFIG,
            state=state,
            now=NOW,
        )
        self.assertEqual(decision.action, "dispatch")
        state["retries"]["forecast"][str(run["id"])]["dispatched"] = True
        state["workflows"]["forecast"]["retry_attempts"] = 1

        retried = decide_retry(
            SPECS["forecast"],
            run,
            classify_failure(workflow="forecast", error_text="rate limit"),
            config=self.CONFIG,
            state=state,
            now=NOW + timedelta(minutes=20),
        )
        self.assertEqual(retried.action, "already_retried")


class MissedScheduleTests(unittest.TestCase):
    """Missed-schedule detection uses freshness SLOs as the source of truth."""

    SLO = default_slo_config()

    def test_freshness_breach_marks_schedule_missed(self) -> None:
        spec = SPECS["forecast"]
        result = check_missed_schedule(
            spec,
            self.SLO,
            runs=[_run(run_id=2, started=_iso(NOW - timedelta(minutes=10)))],
            freshness={"production_forecast": 4.0},
            now=NOW,
        )
        self.assertTrue(result.missed)
        self.assertEqual(result.signal, "freshness_slo")

    def test_freshness_healthy_beats_stale_run(self) -> None:
        spec = SPECS["forecast"]
        result = check_missed_schedule(
            spec,
            self.SLO,
            runs=[_run(run_id=2, started=_iso(NOW - timedelta(hours=10)))],
            freshness={"production_forecast": 0.5},
            now=NOW,
        )
        self.assertFalse(result.missed)
        self.assertEqual(result.signal, "freshness_slo")

    def test_workflow_run_within_threshold_is_healthy(self) -> None:
        spec = SPECS["history_audit"]
        result = check_missed_schedule(
            spec,
            self.SLO,
            runs=[_run(run_id=2, started=_iso(NOW - timedelta(hours=5)))],
            now=NOW,
        )
        self.assertFalse(result.missed)
        self.assertEqual(result.signal, "workflow_run")

    def test_stale_workflow_run_is_missed(self) -> None:
        spec = SPECS["history_audit"]
        result = check_missed_schedule(
            spec,
            self.SLO,
            runs=[_run(run_id=2, started=_iso(NOW - timedelta(hours=40)))],
            now=NOW,
        )
        self.assertTrue(result.missed)
        self.assertEqual(result.signal, "workflow_run")

    def test_no_runs_is_missed(self) -> None:
        spec = SPECS["drill"]
        result = check_missed_schedule(spec, self.SLO, runs=[], now=NOW)
        self.assertTrue(result.missed)
        self.assertEqual(result.signal, "no_runs")


class EscalationDedupTests(unittest.TestCase):
    """Table-driven deduplication of incident escalation."""

    def test_first_escalation_creates_incident(self) -> None:
        state = _fresh_state()
        esc = decide_escalation(
            incident_key("forecast", "validation"),
            workflow="forecast",
            category="validation",
            message="boom",
            now=NOW,
            state=state,
            dedup_minutes=120,
        )
        self.assertEqual(esc.action, "create")
        self.assertEqual(esc.alert_count, 1)

    def test_duplicate_within_window_is_suppressed(self) -> None:
        state = _fresh_state()
        decide_escalation(
            incident_key("forecast", "validation"),
            workflow="forecast",
            category="validation",
            message="boom",
            now=NOW,
            state=state,
            dedup_minutes=120,
        )
        esc = decide_escalation(
            incident_key("forecast", "validation"),
            workflow="forecast",
            category="validation",
            message="boom again",
            now=NOW + timedelta(minutes=5),
            state=state,
            dedup_minutes=120,
        )
        self.assertEqual(esc.action, "suppressed")
        self.assertEqual(esc.alert_count, 1)

    def test_reopened_after_window_is_annotated(self) -> None:
        state = _fresh_state()
        decide_escalation(
            incident_key("forecast", "validation"),
            workflow="forecast",
            category="validation",
            message="boom",
            now=NOW,
            state=state,
            dedup_minutes=120,
        )
        esc = decide_escalation(
            incident_key("forecast", "validation"),
            workflow="forecast",
            category="validation",
            message="boom again",
            now=NOW + timedelta(minutes=121),
            state=state,
            dedup_minutes=120,
        )
        self.assertEqual(esc.action, "update")
        self.assertEqual(esc.alert_count, 2)

    def test_recovered_incident_creates_fresh_escalation(self) -> None:
        state = _fresh_state()
        decide_escalation(
            incident_key("forecast", "validation"),
            workflow="forecast",
            category="validation",
            message="boom",
            now=NOW,
            state=state,
            dedup_minutes=120,
        )
        recover_incidents(
            SPECS["forecast"],
            state=state,
            now=NOW + timedelta(minutes=30),
            reason="fixed",
        )
        esc = decide_escalation(
            incident_key("forecast", "validation"),
            workflow="forecast",
            category="validation",
            message="boom again",
            now=NOW + timedelta(minutes=40),
            state=state,
            dedup_minutes=120,
        )
        self.assertEqual(esc.action, "create")
        self.assertEqual(esc.alert_count, 1)


class RecoveryTests(unittest.TestCase):
    def test_open_incident_is_recovered(self) -> None:
        state = _fresh_state()
        decide_escalation(
            incident_key("forecast", "validation"),
            workflow="forecast",
            category="validation",
            message="boom",
            now=NOW,
            state=state,
            dedup_minutes=120,
        )
        recoveries = recover_incidents(
            SPECS["forecast"], state=state, now=NOW + timedelta(hours=1), reason="run passed"
        )
        self.assertEqual(len(recoveries), 1)
        self.assertIsInstance(recoveries[0], Recovery)
        self.assertIn("RECOVERED", recoveries[0].message)
        self.assertEqual(
            state["incidents"][incident_key("forecast", "validation")]["state"], "recovered"
        )

    def test_already_recovered_is_not_emitted_again(self) -> None:
        state = _fresh_state()
        decide_escalation(
            incident_key("forecast", "validation"),
            workflow="forecast",
            category="validation",
            message="boom",
            now=NOW,
            state=state,
            dedup_minutes=120,
        )
        recover_incidents(SPECS["forecast"], state=state, now=NOW, reason="run passed")
        recoveries = recover_incidents(
            SPECS["forecast"], state=state, now=NOW + timedelta(hours=1), reason="run passed"
        )
        self.assertEqual(recoveries, [])

    def test_recovery_from_missed_schedule(self) -> None:
        state = _fresh_state()
        decide_escalation(
            incident_key("pages", "missed_schedule"),
            workflow="pages",
            category="missed_schedule",
            message="site stale",
            now=NOW,
            state=state,
            dedup_minutes=120,
        )
        recoveries = recover_incidents(
            SPECS["pages"],
            state=state,
            now=NOW + timedelta(hours=1),
            reason="schedule freshness restored",
            categories=("missed_schedule",),
        )
        self.assertEqual(len(recoveries), 1)
        self.assertEqual(recoveries[0].category, "missed_schedule")


class LatestRunTests(unittest.TestCase):
    def test_latest_run_sorts_by_started_at(self) -> None:
        runs = [
            _run(run_id=1, started=_iso(NOW - timedelta(hours=2))),
            _run(run_id=2, started=_iso(NOW - timedelta(minutes=5))),
        ]
        latest = _latest_run(runs)
        self.assertIsNotNone(latest)
        self.assertEqual(int(latest["id"]), 2)  # type: ignore[index]

    def test_empty_runs_return_none(self) -> None:
        self.assertIsNone(_latest_run([]))


class RunGuardIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _paths(self) -> tuple[Path, Path, Path]:
        return (
            self.root / "state.json",
            self.root / "report.json",
            self.root / "events.jsonl",
        )

    def test_persistent_failure_escalates_and_dedups(self) -> None:
        state, report, events = self._paths()
        runs = [_run(run_id=10, conclusion="failure", error_text="validation failed")]
        actions: dict[str, object] = {
            "created": [],
        }

        def record_create(title: str, body: str, labels: tuple[str, ...]) -> str:
            actions["created"].append(title)
            return "https://github.com/owner/repo/issues/42"

        result1 = run_guard(
            GuardianConfig(),
            default_slo_config(),
            runs_by_workflow={"forecast": runs},
            workflows=["forecast"],
            now=NOW,
            state_path=state,
            report_path=report,
            event_log_path=events,
            emit_issues=True,
            repo="owner/repo",
            actions=GuardianActions(create_issue=record_create, fetch_runs=lambda wf: runs),
        )
        self.assertEqual(result1["totals"]["escalations"], 1)
        self.assertEqual(result1["totals"]["recoveries"], 0)
        self.assertEqual(len(actions["created"]), 1)

        result2 = run_guard(
            GuardianConfig(),
            default_slo_config(),
            runs_by_workflow={"forecast": runs},
            workflows=["forecast"],
            now=NOW + timedelta(minutes=5),
            state_path=state,
            report_path=report,
            event_log_path=events,
            emit_issues=True,
            repo="owner/repo",
            actions=GuardianActions(create_issue=record_create, fetch_runs=lambda wf: runs),
        )
        self.assertEqual(result2["totals"]["escalations"], 0)
        self.assertEqual(result2["totals"]["suppressed"], 0)
        self.assertEqual(len(actions["created"]), 1, "no duplicate issue for the same run")

    def test_transient_failure_dispatches_retry_once(self) -> None:
        state, report, events = self._paths()
        started = _iso(NOW - timedelta(minutes=30))
        runs = [_run(run_id=11, conclusion="failure", started=started, error_text="rate limit")]
        dispatched: list[str] = []

        result = run_guard(
            GuardianConfig(),
            default_slo_config(),
            runs_by_workflow={"forecast": runs},
            workflows=["forecast"],
            now=NOW,
            state_path=state,
            report_path=report,
            event_log_path=events,
            actions=GuardianActions(
                fetch_runs=lambda wf: runs, dispatch=lambda wf: (dispatched.append(wf), True)[1]
            ),
        )
        self.assertEqual(result["totals"]["retries_dispatched"], 1)
        self.assertEqual(dispatched, ["forecast.yml"])
        self.assertEqual(result["totals"]["escalations"], 0)

    def test_recovery_after_escalated_run_succeeds(self) -> None:
        state, report, events = self._paths()
        failed = _run(run_id=20, conclusion="failure", error_text="validation failed")
        passed = _run(run_id=21, conclusion="success", started=_iso(NOW - timedelta(minutes=5)))
        created: list[str] = []

        def record_create(title: str, body: str, labels: tuple[str, ...]) -> str:
            created.append(title)
            return "https://github.com/owner/repo/issues/7"

        first = run_guard(
            GuardianConfig(),
            default_slo_config(),
            runs_by_workflow={"forecast": [failed]},
            workflows=["forecast"],
            now=NOW,
            state_path=state,
            report_path=report,
            event_log_path=events,
            emit_issues=True,
            repo="owner/repo",
            actions=GuardianActions(create_issue=record_create, fetch_runs=lambda wf: [failed]),
        )
        self.assertEqual(first["totals"]["escalations"], 1)
        self.assertEqual(first["totals"]["recoveries"], 0)

        second = run_guard(
            GuardianConfig(),
            default_slo_config(),
            runs_by_workflow={"forecast": [passed]},
            workflows=["forecast"],
            now=NOW + timedelta(minutes=10),
            state_path=state,
            report_path=report,
            event_log_path=events,
            emit_issues=True,
            repo="owner/repo",
            actions=GuardianActions(create_issue=record_create, fetch_runs=lambda wf: [passed]),
        )
        self.assertEqual(second["totals"]["recoveries"], 1)
        self.assertEqual(second["totals"]["escalations"], 0)

    def test_missed_schedule_escalates_via_freshness_slo(self) -> None:
        state, report, events = self._paths()
        result = run_guard(
            GuardianConfig(),
            default_slo_config(),
            runs_by_workflow={"pages": [_run(run_id=30)]},
            workflows=["pages"],
            freshness={"site_update": 5.0},
            now=NOW,
            state_path=state,
            report_path=report,
            event_log_path=events,
        )
        self.assertEqual(result["totals"]["missed_schedules"], 1)
        self.assertEqual(result["totals"]["escalations"], 1)
        self.assertEqual(result["escalations"][0]["category"], "missed_schedule")

    def test_report_is_persisted(self) -> None:
        state, report, events = self._paths()
        run_guard(
            GuardianConfig(),
            default_slo_config(),
            runs_by_workflow={"forecast": [_run(run_id=1)]},
            workflows=["forecast"],
            now=NOW,
            state_path=state,
            report_path=report,
            event_log_path=events,
        )
        self.assertTrue(report.exists())
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 1)
        self.assertIn("totals", payload)


if __name__ == "__main__":
    unittest.main()
