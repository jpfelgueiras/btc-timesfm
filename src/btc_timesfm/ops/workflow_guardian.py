#!/usr/bin/env python3
"""Workflow guardian watchdog for scheduled GitHub Actions workflows.

Runnable entry point: python -m btc_timesfm.ops.workflow_guardian

Watches the forecast, hourly dispatcher, optimizer, history-audit, drill and
pages workflows through workflow-run signals (``gh api`` run history) and
freshness SLO signals. Failures are classified as transient (rate limit,
quiesced runner, network, upstream API) or persistent. Transient failures are
auto-retried once with backoff; persistent failures and missed schedules
escalate into a tracked GitHub issue with a runbook link. Escalations are
deduplicated per incident so thousands of runs never produce alert storms, and
a recovery message is emitted when an incident resolves.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Sequence

from btc_timesfm.ops.freshness_slo import (
    DEFAULT_SLO_PATH,
    FreshnessSLOConfig,
    _iso,
    _parse_timestamp,
    _utc_now,
    load_config,
)
from btc_timesfm.ops.freshness_watchdog import emit_github_issue, fetch_workflow_runs


STATE_VERSION = 1
STATE_PATH = Path(".state/workflow_guardian.json")
REPORT_PATH = Path("workflow_guardian_report.json")
EVENT_LOG_PATH = Path("workflow_guardian_events.jsonl")
DEFAULT_FRESHNESS_STATUS_PATH = Path("freshness_slo_status.json")
DEDUP_PREFIX = "workflow-incident"
REPO_ENV = "GITHUB_REPOSITORY"
RUNBOOK_URL = "https://github.com/jpfelgueiras/btc-timesfm/blob/main/docs/WORKFLOW_GUARDIAN.md"
ISSUE_LABELS = ("workflow-guardian", "alert")
SUCCESS_CONCLUSIONS = {"success", "skipped", ""}
FAILURE_CONCLUSIONS = {"failure", "timed_out", "action_required", "cancelled", "startup_failure"}
FAILURE_STATUSES = {"failed", "timed_out", "action_required"}

# Markers used to classify a failed run. Order matters: the first matching
# transient/upset marker wins.
TRANSIENT_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "rate_limit",
        (
            "rate limit",
            "secondary rate limit",
            "api rate limit",
            "abuse detection",
            "too many requests",
            "http 429",
            "429",
        ),
    ),
    (
        "runner_quiesced",
        (
            "runner has been terminated",
            "runner is shutting down",
            "runner was shutdown",
            "runner offline",
            "quiesc",
            "terminated by",
            "coalescing",
        ),
    ),
    (
        "network",
        (
            "connection",
            "socket",
            "temporarily unavailable",
            "name or service not known",
            "could not resolve",
            "ssl",
            "tls handshake",
            "connection reset",
            "broken pipe",
            "eof",
            "operation timed out",
            "request timed out",
            "network error",
        ),
    ),
    (
        "upstream_api",
        (
            "502",
            "503",
            "504",
            "529",
            "bad gateway",
            "service unavailable",
            "gateway timeout",
            "upstream",
            "internal server error",
            "unexpected error",
            "api error",
        ),
    ),
)

PERSISTENT_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "validation",
        (
            "validation failed",
            "validation_error",
            "schema validation",
            "assertionerror",
            "assert ",
        ),
    ),
    (
        "code_error",
        (
            "keyerror",
            "typeerror",
            "valueerror",
            "indexerror",
            "attributeerror",
            "zerodivisionerror",
            "modulenotfounderror",
            "importerror",
            "traceback (most recent call last)",
            "syntaxerror",
        ),
    ),
    (
        "data_error",
        (
            "data mismatch",
            "integrity",
            "corrupt",
            "audit failed",
            "failed audit",
            "backup failed",
            "restore failed",
        ),
    ),
)


@dataclass(frozen=True)
class WorkflowSpec:
    """One scheduled workflow the guardian watches."""

    name: str
    workflow_file: str
    display_name: str
    cron: str
    cadence_hours: float
    timeout_minutes: int = 60
    freshness_metric: str | None = None
    missed_after_hours: float | None = None
    runbook_url: str = RUNBOOK_URL

    def __post_init__(self) -> None:
        if not self.name or not self.workflow_file:
            raise ValueError("workflow name and file must be non-empty")
        if self.cadence_hours <= 0:
            raise ValueError(f"cadence_hours must be positive for {self.name}")
        if self.timeout_minutes <= 0:
            raise ValueError(f"timeout_minutes must be positive for {self.name}")
        if self.missed_after_hours is not None and self.missed_after_hours <= 0:
            raise ValueError(f"missed_after_hours must be positive for {self.name}")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_workflows() -> tuple[WorkflowSpec, ...]:
    """The full table of scheduled workflows with cadence and retry policy."""
    return (
        WorkflowSpec(
            name="forecast",
            workflow_file="forecast.yml",
            display_name="BTC TimesFM 3 Forecast",
            cron="37 */2 * * *",
            cadence_hours=2.0,
            timeout_minutes=20,
            freshness_metric="production_forecast",
        ),
        WorkflowSpec(
            name="hourly_dispatcher",
            workflow_file="hourly-forecast.yml",
            display_name="Hourly BTC Forecast Dispatcher",
            cron="7 * * * *",
            cadence_hours=1.0,
            timeout_minutes=5,
            freshness_metric="scheduled_run",
        ),
        WorkflowSpec(
            name="pages",
            workflow_file="pages.yml",
            display_name="BTC Forecast GitHub Pages",
            cron="17 * * * *",
            cadence_hours=1.0,
            timeout_minutes=10,
            freshness_metric="site_update",
        ),
        WorkflowSpec(
            name="optimizer",
            workflow_file="optimize.yml",
            display_name="Weekly Forecast Optimizer",
            cron="17 4 * * 0",
            cadence_hours=168.0,
            timeout_minutes=60,
            missed_after_hours=216.0,
        ),
        WorkflowSpec(
            name="history_audit",
            workflow_file="history-audit.yml",
            display_name="Forecast history integrity audit",
            cron="17 5 * * *",
            cadence_hours=24.0,
            timeout_minutes=5,
            missed_after_hours=36.0,
        ),
        WorkflowSpec(
            name="drill",
            workflow_file="disaster-recovery-drill.yml",
            display_name="Forecast history disaster-recovery drill",
            cron="43 6 * * 1",
            cadence_hours=168.0,
            timeout_minutes=10,
            missed_after_hours=216.0,
        ),
    )


@dataclass(frozen=True)
class GuardianConfig:
    """Retry and deduplication policy shared by all watched workflows."""

    workflows: tuple[WorkflowSpec, ...] = field(default_factory=default_workflows)
    dedup_minutes: int = 120
    retry_attempts: int = 1
    retry_backoff_minutes: int = 15
    runbook_url: str = RUNBOOK_URL
    issue_labels: tuple[str, ...] = ISSUE_LABELS

    def __post_init__(self) -> None:
        if self.dedup_minutes <= 0:
            raise ValueError("dedup_minutes must be positive")
        if self.retry_attempts < 0:
            raise ValueError("retry_attempts must be >= 0")
        if self.retry_backoff_minutes < 0:
            raise ValueError("retry_backoff_minutes must be >= 0")


@dataclass(frozen=True)
class FailureClassification:
    """Result of classifying a failed run."""

    kind: str
    category: str
    retryable: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_failure(
    *,
    workflow: str,
    conclusion: str = "",
    error_text: str = "",
    status: str = "",
) -> FailureClassification:
    """Classify a failed run as transient or persistent.

    Transient categories (rate limit, runner quiesced, network, upstream API)
    are auto-retried once. Everything else is treated as persistent so the
    incident can be escalated instead of silently retried forever.
    """
    text = " ".join(part.lower() for part in (error_text, conclusion, str(status)) if part)
    for category, markers in TRANSIENT_PATTERNS:
        if any(marker in text for marker in markers):
            return FailureClassification(
                kind="transient",
                category=category,
                retryable=True,
                reason=f"transient:{category}",
            )
    for category, markers in PERSISTENT_PATTERNS:
        if any(marker in text for marker in markers):
            return FailureClassification(
                kind="persistent",
                category=category,
                retryable=False,
                reason=f"persistent:{category}",
            )
    return FailureClassification(
        kind="persistent",
        category="unknown",
        retryable=False,
        reason=f"persistent:unknown ({workflow})",
    )


@dataclass(frozen=True)
class MissedCheck:
    """Result of a missed-schedule check for one workflow."""

    workflow: str
    missed: bool
    threshold_hours: float
    signal: str
    age_hours: float | None
    last_seen_at: str | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _missed_after_hours(spec: WorkflowSpec, slo_config: FreshnessSLOConfig) -> float:
    """Derive the missed-schedule threshold from the freshness SLO source of truth."""
    if spec.freshness_metric:
        metric = slo_config.metric_by_name(spec.freshness_metric)
        if metric is not None:
            return metric.critical_tolerance_hours
    if spec.missed_after_hours is not None:
        return spec.missed_after_hours
    return spec.cadence_hours * 1.5


def check_missed_schedule(
    spec: WorkflowSpec,
    slo_config: FreshnessSLOConfig,
    *,
    runs: Sequence[dict[str, Any]] | None = None,
    freshness: dict[str, float] | None = None,
    now: datetime | None = None,
) -> MissedCheck:
    """Detect a missed schedule using freshness SLOs as the source of truth.

    When a workflow maps to a freshness metric, that SLO decides: a breach
    means the schedule is missed and a healthy value means it is on schedule.
    Workflows without a freshness metric fall back to the latest workflow-run
    timestamp compared against the configured cadence.
    """
    checked_at = now or _utc_now()
    freshness = freshness or {}
    run_list = [r for r in (runs or []) if isinstance(r, dict)]
    latest = _latest_run(run_list)
    latest_at = _parse_timestamp(
        (latest or {}).get("run_started_at") or (latest or {}).get("created_at")
    )
    threshold = _missed_after_hours(spec, slo_config)

    if spec.freshness_metric:
        metric = slo_config.metric_by_name(spec.freshness_metric)
        if metric is not None and spec.freshness_metric in freshness:
            age = freshness[spec.freshness_metric]
            if age is not None and age > metric.critical_tolerance_hours:
                return MissedCheck(
                    workflow=spec.name,
                    missed=True,
                    threshold_hours=metric.critical_tolerance_hours,
                    signal="freshness_slo",
                    age_hours=round(float(age), 2),
                    last_seen_at=_iso(latest_at) if latest_at is not None else None,
                    message=(
                        f"Freshness SLO '{spec.freshness_metric}' is {age:.1f}h old "
                        f"(critical: {metric.critical_tolerance_hours}h) — "
                        f"'{spec.name}' has missed its schedule"
                    ),
                )
            return MissedCheck(
                workflow=spec.name,
                missed=False,
                threshold_hours=metric.critical_tolerance_hours,
                signal="freshness_slo",
                age_hours=round(float(age), 2),
                last_seen_at=_iso(latest_at) if latest_at is not None else None,
                message=f"Freshness SLO '{spec.freshness_metric}' within tolerance",
            )

    if latest is None:
        return MissedCheck(
            workflow=spec.name,
            missed=True,
            threshold_hours=threshold,
            signal="no_runs",
            age_hours=None,
            last_seen_at=None,
            message=f"No workflow runs observed for '{spec.name}'",
        )
    if latest_at is None:
        return MissedCheck(
            workflow=spec.name,
            missed=False,
            threshold_hours=threshold,
            signal="unknown",
            age_hours=None,
            last_seen_at=None,
            message=f"Cannot determine schedule freshness for '{spec.name}'",
        )
    age_hours = (checked_at - latest_at).total_seconds() / 3600.0
    if age_hours > threshold:
        return MissedCheck(
            workflow=spec.name,
            missed=True,
            threshold_hours=threshold,
            signal="workflow_run",
            age_hours=round(age_hours, 2),
            last_seen_at=_iso(latest_at),
            message=(
                f"Latest '{spec.name}' run is {age_hours:.1f}h old "
                f"(threshold: {threshold}h) — schedule may be missed"
            ),
        )
    return MissedCheck(
        workflow=spec.name,
        missed=False,
        threshold_hours=threshold,
        signal="workflow_run",
        age_hours=round(age_hours, 2),
        last_seen_at=_iso(latest_at),
        message=f"Latest '{spec.name}' run is {age_hours:.1f}h old",
    )


@dataclass
class RetryDecision:
    """Decision about whether to retry a transient failure."""

    workflow: str
    run_id: str
    classification: FailureClassification
    action: str
    dispatched: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow,
            "run_id": self.run_id,
            "classification": self.classification.reason,
            "category": self.classification.category,
            "action": self.action,
            "dispatched": self.dispatched,
            "reason": self.reason,
        }


def decide_retry(
    spec: WorkflowSpec,
    run: dict[str, Any],
    classification: FailureClassification,
    *,
    config: GuardianConfig,
    state: dict[str, Any],
    now: datetime,
) -> RetryDecision:
    """Decide whether a transient failure gets an automatic retry with backoff."""
    run_id = str(run.get("id") or "")
    if not classification.retryable:
        return RetryDecision(
            spec.name, run_id, classification, "not_retryable", False, classification.reason
        )
    monitor = _ensure_monitor(state, spec.name)
    attempts = int(monitor.get("retry_attempts", 0))
    if attempts >= int(config.retry_attempts):
        return RetryDecision(
            spec.name,
            run_id,
            classification,
            "already_retried",
            False,
            f"retry budget exhausted ({attempts}/{config.retry_attempts})",
        )
    retry_map = state.setdefault("retries", {}).setdefault(spec.name, {})
    entry = retry_map.get(run_id)
    if isinstance(entry, dict) and entry.get("dispatched"):
        return RetryDecision(
            spec.name,
            run_id,
            classification,
            "already_retried",
            True,
            f"run {run_id} already retried",
        )
    started_at = _parse_timestamp(run.get("created_at") or run.get("run_started_at"))
    base = started_at or now
    due_at = base + timedelta(minutes=int(config.retry_backoff_minutes))
    retry_map[run_id] = {"dispatched": False, "due_at": _iso(due_at)}
    if now < due_at:
        return RetryDecision(
            spec.name,
            run_id,
            classification,
            "deferred",
            False,
            f"transient {classification.reason}; retry scheduled at {_iso(due_at)}",
        )
    return RetryDecision(
        spec.name,
        run_id,
        classification,
        "dispatch",
        False,
        f"transient {classification.reason}; retrying once",
    )


def incident_key(workflow: str, category: str) -> str:
    return f"{DEDUP_PREFIX}:{workflow}:{category}"


@dataclass
class Escalation:
    """Escalation decision, deduplicated against already-open incidents."""

    incident_key: str
    workflow: str
    category: str
    action: str
    issue_url: str | None
    issue_number: int | None
    alert_count: int
    opened_at: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "incident_key": self.incident_key,
            "workflow": self.workflow,
            "category": self.category,
            "action": self.action,
            "issue_url": self.issue_url,
            "issue_number": self.issue_number,
            "alert_count": self.alert_count,
            "opened_at": self.opened_at,
            "message": self.message,
        }


def decide_escalation(
    key: str,
    *,
    workflow: str,
    category: str,
    message: str,
    now: datetime,
    state: dict[str, Any],
    dedup_minutes: int,
    issue_url: str | None = None,
    issue_number: int | None = None,
) -> Escalation:
    """Create a tracked incident, annotate an open one, or suppress a duplicate."""
    incidents = state.setdefault("incidents", {})
    entry = incidents.get(key)
    if not isinstance(entry, dict) or entry.get("state") != "open":
        incidents[key] = {
            "workflow": workflow,
            "category": category,
            "state": "open",
            "issue_url": issue_url,
            "issue_number": issue_number,
            "opened_at": _iso(now),
            "last_seen_at": _iso(now),
            "alert_count": 1,
        }
        return Escalation(
            incident_key=key,
            workflow=workflow,
            category=category,
            action="create",
            issue_url=issue_url,
            issue_number=issue_number,
            alert_count=1,
            opened_at=_iso(now),
            message=message,
        )
    last_seen = _parse_timestamp(entry.get("last_seen_at"))
    count = int(entry.get("alert_count", 0))
    if last_seen is not None and (now - last_seen).total_seconds() / 60.0 < dedup_minutes:
        entry["last_seen_at"] = _iso(now)
        return Escalation(
            incident_key=key,
            workflow=workflow,
            category=category,
            action="suppressed",
            issue_url=str(entry.get("issue_url") or None),
            issue_number=_issue_number(entry),
            alert_count=count,
            opened_at=str(entry.get("opened_at") or _iso(now)),
            message=message,
        )
    entry["last_seen_at"] = _iso(now)
    entry["alert_count"] = count + 1
    return Escalation(
        incident_key=key,
        workflow=workflow,
        category=category,
        action="update",
        issue_url=str(entry.get("issue_url") or None),
        issue_number=_issue_number(entry),
        alert_count=count + 1,
        opened_at=str(entry.get("opened_at") or _iso(now)),
        message=message,
    )


@dataclass
class Recovery:
    """Recovery message emitted when an incident resolves."""

    incident_key: str
    workflow: str
    category: str
    action: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def recover_incidents(
    spec: WorkflowSpec,
    *,
    state: dict[str, Any],
    now: datetime,
    reason: str,
    categories: Sequence[str] | None = None,
) -> list[Recovery]:
    """Mark open incidents recovered for a workflow and build recovery messages."""
    recovered: list[Recovery] = []
    match_categories = set(categories) if categories is not None else None
    for key, entry in list(state.get("incidents", {}).items()):
        if not isinstance(entry, dict):
            continue
        if entry.get("workflow") != spec.name or entry.get("state") != "open":
            continue
        category = str(entry.get("category", ""))
        if match_categories is not None and category not in match_categories:
            continue
        entry["state"] = "recovered"
        entry["recovered_at"] = _iso(now)
        recovered.append(
            Recovery(
                incident_key=key,
                workflow=spec.name,
                category=category,
                action="close",
                message=(
                    f"[RECOVERED] workflow '{spec.name}' incident '{category}' resolved: {reason}"
                ),
            )
        )
    return recovered


@dataclass
class GuardianActions:
    """Overridable I/O the guardian performs (defaults to gh CLI calls)."""

    fetch_runs: Callable[[str], list[dict[str, Any]]] | None = None
    dispatch: Callable[[str], bool] | None = None
    create_issue: Callable[[str, str, tuple[str, ...]], str | None] | None = None
    comment: Callable[[int, str], bool] | None = None


def _fresh_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "incidents": {},
        "workflows": {},
        "retries": {},
        "last_run_at": None,
    }


def _monitor_default(name: str) -> dict[str, Any]:
    return {
        "last_seen_run_id": None,
        "consecutive_failures": 0,
        "retry_attempts": 0,
        "last_success_at": None,
    }


def _ensure_monitor(state: dict[str, Any], name: str) -> dict[str, Any]:
    workflows = state.setdefault("workflows", {})
    entry = workflows.get(name)
    if not isinstance(entry, dict):
        entry = _monitor_default(name)
        workflows[name] = entry
    return entry


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _fresh_state()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _fresh_state()
    if not isinstance(payload, dict):
        return _fresh_state()
    payload.setdefault("incidents", {})
    payload.setdefault("workflows", {})
    payload.setdefault("retries", {})
    return payload


def _save_state(state: dict[str, Any], path: Path) -> None:
    state["last_run_at"] = _iso(_utc_now())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _record_event(
    event: str,
    *,
    event_log_path: Path = EVENT_LOG_PATH,
    workflow: str | None = None,
    run_id: str | None = None,
    **fields: Any,
) -> None:
    payload: dict[str, Any] = {
        "timestamp": _iso(_utc_now()),
        "event": event,
        "workflow": workflow,
        "run_id": run_id,
    }
    payload.update(fields)
    event_log_path.parent.mkdir(parents=True, exist_ok=True)
    with event_log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    print(json.dumps(payload, sort_keys=True))


def _latest_run(runs: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [r for r in runs if isinstance(r, dict)]
    if not candidates:
        return None

    def sort_key(run: dict[str, Any]) -> str:
        stamp = _parse_timestamp(run.get("run_started_at") or run.get("created_at"))
        return stamp.isoformat() if stamp is not None else ""

    candidates.sort(key=sort_key, reverse=True)
    return candidates[0]


def _run_failed(run: dict[str, Any]) -> bool:
    conclusion = str(run.get("conclusion") or "")
    status = str(run.get("status") or "")
    return conclusion in FAILURE_CONCLUSIONS or status in FAILURE_STATUSES


def _run_error_text(run: dict[str, Any]) -> str:
    for key in ("error_text", "failure_reason", "failure_message", "message", "notes"):
        value = run.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _issue_number(entry: dict[str, Any]) -> int | None:
    value = entry.get("issue_number")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _issue_number_from_url(url: str | None) -> int | None:
    if not url:
        return None
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _issue_title(spec: WorkflowSpec, escalation: Escalation) -> str:
    return f"Workflow '{spec.name}' failing: {escalation.category}"


def _issue_body(spec: WorkflowSpec, escalation: Escalation, config: GuardianConfig) -> str:
    return "\n".join(
        [
            "## Workflow guardian incident",
            "",
            f"- Workflow: `{spec.workflow_file}` ({spec.display_name})",
            f"- Scheduled cron: `{spec.cron}`",
            f"- Incident: `{escalation.category}`",
            f"- First seen: `{escalation.opened_at}`",
            "",
            f"**{escalation.message}**",
            "",
            f"Runbook: {config.runbook_url}",
            "",
            "This issue was filed automatically by the workflow guardian watchdog.",
        ]
    )


def _issue_comment(escalation: Escalation) -> str:
    return f"Still failing after {escalation.alert_count} alert(s): {escalation.message}"


def _resolve_repo(repo: str | None) -> str | None:
    return repo or os.getenv(REPO_ENV)


def gh_dispatch_workflow(workflow_file: str, *, repo: str) -> bool:
    try:
        completed = subprocess.run(
            ["gh", "workflow", "run", workflow_file, "--repo", repo, "--ref", "main"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return completed.returncode == 0


def gh_comment_issue(number: int, body: str, *, repo: str) -> bool:
    try:
        completed = subprocess.run(
            ["gh", "issue", "comment", str(number), "--repo", repo, "--body", body],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return completed.returncode == 0


def _dispatch_retry(spec: WorkflowSpec, actions: GuardianActions, repo: str | None) -> bool:
    if actions.dispatch is not None:
        return bool(actions.dispatch(spec.workflow_file))
    if not repo:
        return False
    return gh_dispatch_workflow(spec.workflow_file, repo=repo)


def _escalate_issue(
    escalation: Escalation,
    spec: WorkflowSpec,
    config: GuardianConfig,
    *,
    emit_issues: bool,
    repo: str | None,
    actions: GuardianActions,
    state: dict[str, Any],
) -> None:
    if not emit_issues:
        return
    if escalation.action == "create":
        url = None
        if actions.create_issue is not None:
            url = actions.create_issue(
                _issue_title(spec, escalation),
                _issue_body(spec, escalation, config),
                config.issue_labels,
            )
        else:
            url = emit_github_issue(
                _issue_title(spec, escalation),
                _issue_body(spec, escalation, config),
                repo=repo,
                labels=config.issue_labels,
            )
        if url:
            escalation.issue_url = url
            escalation.issue_number = _issue_number_from_url(url)
    elif escalation.action == "update" and escalation.issue_number is not None:
        ok = False
        if actions.comment is not None:
            ok = actions.comment(escalation.issue_number, _issue_comment(escalation))
        elif repo:
            ok = gh_comment_issue(escalation.issue_number, _issue_comment(escalation), repo=repo)
        if ok:
            _record_event(
                "workflow_incident_annotated",
                workflow=spec.name,
                incident=escalation.incident_key,
                issue_number=escalation.issue_number,
            )
    entry = state.get("incidents", {}).get(escalation.incident_key)
    if isinstance(entry, dict):
        if escalation.issue_url:
            entry["issue_url"] = escalation.issue_url
        if escalation.issue_number is not None:
            entry["issue_number"] = escalation.issue_number


def _comment_recovery(
    recovery: Recovery,
    *,
    emit_issues: bool,
    repo: str | None,
    actions: GuardianActions,
    state: dict[str, Any],
) -> None:
    if not emit_issues:
        return
    entry = state.get("incidents", {}).get(recovery.incident_key)
    number = _issue_number(entry) if isinstance(entry, dict) else None
    if number is None:
        return
    if actions.comment is not None:
        actions.comment(number, recovery.message)
    elif repo:
        gh_comment_issue(number, recovery.message, repo=repo)


def _load_freshness_status(path: Path) -> dict[str, float]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[str, float] = {}
    for item in payload.get("results", []):
        if not isinstance(item, dict):
            continue
        name = item.get("metric_name")
        hours = item.get("measured_hours")
        if isinstance(name, str) and name and isinstance(hours, (int, float)):
            result[name] = float(hours)
    return result


def run_guard(
    config: GuardianConfig | None = None,
    slo_config: FreshnessSLOConfig | None = None,
    *,
    runs_by_workflow: dict[str, list[dict[str, Any]]] | None = None,
    freshness: dict[str, float] | None = None,
    workflows: Sequence[str] | None = None,
    now: datetime | None = None,
    state_path: Path = STATE_PATH,
    report_path: Path = REPORT_PATH,
    event_log_path: Path = EVENT_LOG_PATH,
    emit_issues: bool = False,
    repo: str | None = None,
    actions: GuardianActions | None = None,
) -> dict[str, Any]:
    """Run one guardian sweep over all configured workflows.

    Reads workflow-run and freshness signals, classifies failures, auto-retries
    transient failures once with backoff, escalates persistent failures and
    missed schedules into tracked issues, deduplicates escalation and emits
    recovery messages on resolution.
    """
    checked_at = now or _utc_now()
    cfg = config or GuardianConfig()
    slo = slo_config or load_config()
    gh = actions or GuardianActions()
    resolved_repo = _resolve_repo(repo)
    workflow_filter = set(workflows) if workflows is not None else None
    specs = [
        spec for spec in cfg.workflows if workflow_filter is None or spec.name in workflow_filter
    ]

    state = _load_state(state_path)
    runs_by_workflow = runs_by_workflow or {}
    freshness = freshness or {}

    escalations: list[Escalation] = []
    retries: list[RetryDecision] = []
    recoveries: list[Recovery] = []
    missed_checks: list[MissedCheck] = []

    for spec in specs:
        monitor = _ensure_monitor(state, spec.name)
        runs = _runs_for(spec, runs_by_workflow, gh, resolved_repo)
        latest = _latest_run(runs)
        run_id = str((latest or {}).get("id") or "")
        last_seen = str(monitor.get("last_seen_run_id") or "")
        new_run = bool(run_id) and run_id != last_seen

        missed = check_missed_schedule(spec, slo, runs=runs, freshness=freshness, now=checked_at)
        missed_checks.append(missed)
        if missed.missed:
            esc = decide_escalation(
                incident_key(spec.name, "missed_schedule"),
                workflow=spec.name,
                category="missed_schedule",
                message=missed.message,
                now=checked_at,
                state=state,
                dedup_minutes=cfg.dedup_minutes,
            )
            escalations.append(esc)
            _escalate_issue(
                esc, spec, cfg, emit_issues=emit_issues, repo=resolved_repo, actions=gh, state=state
            )
            _record_event(
                "workflow_missed_schedule",
                event_log_path=event_log_path,
                workflow=spec.name,
                signal=missed.signal,
                action=esc.action,
                age_hours=missed.age_hours,
                message=missed.message,
            )
        elif not missed.missed:
            recoveries.extend(
                recover_incidents(
                    spec,
                    state=state,
                    now=checked_at,
                    reason="schedule freshness restored",
                    categories=("missed_schedule",),
                )
            )

        if latest is None:
            continue

        if not _run_failed(latest):
            monitor["consecutive_failures"] = 0
            monitor["retry_attempts"] = 0
            monitor["last_success_at"] = _iso(checked_at)
            if new_run:
                monitor["last_seen_run_id"] = run_id
                _record_event(
                    "workflow_run_succeeded",
                    event_log_path=event_log_path,
                    workflow=spec.name,
                    run_id=run_id,
                )
            new_recoveries = recover_incidents(
                spec,
                state=state,
                now=checked_at,
                reason="latest scheduled run succeeded",
            )
            recoveries.extend(new_recoveries)
            continue

        classification = classify_failure(
            workflow=spec.name,
            conclusion=str(latest.get("conclusion") or ""),
            status=str(latest.get("status") or ""),
            error_text=_run_error_text(latest),
        )
        retry_map = state.setdefault("retries", {}).setdefault(spec.name, {})
        entry = retry_map.get(run_id)
        pending = isinstance(entry, dict) and not entry.get("dispatched")

        if new_run:
            monitor["last_seen_run_id"] = run_id
        if not new_run and not pending:
            _record_event(
                "workflow_failed_unchanged",
                event_log_path=event_log_path,
                workflow=spec.name,
                run_id=run_id,
                classification=classification.reason,
            )
            continue

        if new_run:
            monitor["consecutive_failures"] = int(monitor.get("consecutive_failures", 0)) + 1

        decision = decide_retry(
            spec,
            latest,
            classification,
            config=cfg,
            state=state,
            now=checked_at,
        )
        retries.append(decision)
        _record_event(
            "workflow_failed",
            event_log_path=event_log_path,
            workflow=spec.name,
            run_id=run_id,
            classification=classification.reason,
            category=classification.category,
            decision=decision.action,
            consecutive_failures=int(monitor.get("consecutive_failures", 0)),
            retry_attempts=int(monitor.get("retry_attempts", 0)),
        )

        if decision.action == "dispatch":
            monitor["retry_attempts"] = int(monitor.get("retry_attempts", 0)) + 1
            dispatched = _dispatch_retry(spec, gh, resolved_repo)
            retry_map[run_id] = {"dispatched": dispatched, "due_at": _iso(checked_at)}
            if dispatched:
                retries[-1].dispatched = True
            elif monitor["retry_attempts"] > 0:
                monitor["retry_attempts"] -= 1
            _record_event(
                "workflow_retry_dispatched" if dispatched else "workflow_retry_failed",
                event_log_path=event_log_path,
                workflow=spec.name,
                run_id=run_id,
                backoff_minutes=cfg.retry_backoff_minutes,
            )
        elif decision.action == "deferred":
            _record_event(
                "workflow_retry_deferred",
                event_log_path=event_log_path,
                workflow=spec.name,
                run_id=run_id,
                backoff_minutes=cfg.retry_backoff_minutes,
            )
        elif decision.action in ("already_retried", "not_retryable"):
            if not new_run:
                continue
            category = classification.category
            esc = decide_escalation(
                incident_key(spec.name, category),
                workflow=spec.name,
                category=category,
                message=f"Workflow '{spec.name}' failed: {classification.reason}",
                now=checked_at,
                state=state,
                dedup_minutes=cfg.dedup_minutes,
            )
            escalations.append(esc)
            _escalate_issue(
                esc, spec, cfg, emit_issues=emit_issues, repo=resolved_repo, actions=gh, state=state
            )
            _record_event(
                "workflow_escalated",
                event_log_path=event_log_path,
                workflow=spec.name,
                run_id=run_id,
                incident=esc.incident_key,
                category=category,
                action=esc.action,
                alert_count=esc.alert_count,
            )

    for recovery in recoveries:
        _record_event(
            "workflow_recovered",
            event_log_path=event_log_path,
            workflow=recovery.workflow,
            incident=recovery.incident_key,
            category=recovery.category,
            message=recovery.message,
        )
        _comment_recovery(
            recovery,
            emit_issues=emit_issues,
            repo=resolved_repo,
            actions=gh,
            state=state,
        )

    _save_state(state, state_path)

    per_workflow = {
        name: dict(state["workflows"].get(name) or _monitor_default(name))
        for name in sorted(spec.name for spec in cfg.workflows)
        if workflow_filter is None or name in workflow_filter
    }
    summary: dict[str, Any] = {
        "schema_version": STATE_VERSION,
        "checked_at": _iso(checked_at),
        "workflows": per_workflow,
        "missed_schedules": [m.as_dict() for m in missed_checks],
        "retries": [r.as_dict() for r in retries],
        "escalations": [e.as_dict() for e in escalations],
        "recoveries": [r.as_dict() for r in recoveries],
        "totals": {
            "retries_deferred": sum(1 for r in retries if r.action == "deferred"),
            "retries_dispatched": sum(
                1 for r in retries if r.action == "dispatch" and r.dispatched
            ),
            "escalations": sum(1 for e in escalations if e.action in ("create", "update")),
            "suppressed": sum(1 for e in escalations if e.action == "suppressed"),
            "recoveries": len(recoveries),
            "missed_schedules": sum(1 for m in missed_checks if m.missed),
        },
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def _runs_for(
    spec: WorkflowSpec,
    runs_by_workflow: dict[str, list[dict[str, Any]]],
    actions: GuardianActions,
    repo: str | None,
) -> list[dict[str, Any]]:
    if spec.name in runs_by_workflow:
        return runs_by_workflow[spec.name] or []
    if actions.fetch_runs is not None:
        return actions.fetch_runs(spec.workflow_file) or []
    if repo:
        return fetch_workflow_runs(repo, workflow=spec.workflow_file)
    return []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Workflow guardian watchdog for scheduled GitHub Actions workflows"
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_SLO_PATH)
    parser.add_argument("--state", type=Path, default=STATE_PATH)
    parser.add_argument("--report", type=Path, default=REPORT_PATH)
    parser.add_argument("--events", type=Path, default=EVENT_LOG_PATH)
    parser.add_argument("--freshness-status", type=Path, default=DEFAULT_FRESHNESS_STATUS_PATH)
    parser.add_argument("--workflow", action="append", default=[])
    parser.add_argument("--dedup-minutes", type=int, default=None)
    parser.add_argument("--retry-attempts", type=int, default=None)
    parser.add_argument("--retry-backoff-minutes", type=int, default=None)
    parser.add_argument("--no-gh", action="store_true", help="Skip gh API calls")
    parser.add_argument(
        "--emit-issue", action="store_true", help="Create/annotate tracked GitHub issues"
    )
    parser.add_argument("--now", default=None, help="ISO timestamp for deterministic runs")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    valid_names = {spec.name for spec in default_workflows()}
    unknown = [name for name in args.workflow if name not in valid_names]
    if unknown:
        raise SystemExit(f"unknown workflow(s): {', '.join(unknown)}")

    checked_at = _parse_timestamp(args.now) if args.now else _utc_now()
    slo_config = load_config(args.config)
    cfg = GuardianConfig(
        dedup_minutes=args.dedup_minutes if args.dedup_minutes else GuardianConfig().dedup_minutes,
        retry_attempts=(
            args.retry_attempts
            if args.retry_attempts is not None
            else GuardianConfig().retry_attempts
        ),
        retry_backoff_minutes=(
            args.retry_backoff_minutes
            if args.retry_backoff_minutes is not None
            else GuardianConfig().retry_backoff_minutes
        ),
    )
    freshness = {} if args.no_gh else _load_freshness_status(args.freshness_status)
    no_gh_actions = (
        GuardianActions(fetch_runs=lambda _wf: [], dispatch=lambda _wf: False)
        if args.no_gh
        else None
    )
    summary = run_guard(
        cfg,
        slo_config,
        now=checked_at,
        freshness=freshness,
        workflows=args.workflow or None,
        state_path=args.state,
        report_path=args.report,
        event_log_path=args.events,
        emit_issues=args.emit_issue,
        actions=no_gh_actions,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    if summary["totals"]["escalations"] > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
