#!/usr/bin/env python3
"""Freshness SLO watchdog — detects stale forecasts, site, backups and missed runs.

Runnable entry point: python -m btc_timesfm.ops.freshness_watchdog

Checks durable history freshness, last workflow runs via ``gh api``, site
latest-origin/generated-at timestamps, and history backup age. Alerts on SLO
breach with deduplication, runbook links and recovery messages. Breaches and
recoveries are recorded as JSONL metrics.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from btc_timesfm.ops.freshness_slo import (
    DEFAULT_SLO_PATH,
    FreshnessMetric,
    FreshnessSLOConfig,
    _iso,
    _parse_timestamp,
    _utc_now,
    load_config,
)


STATE_PATH = Path(".state/freshness_watchdog.json")
METRICS_LOG_PATH = Path("freshness_slo_events.jsonl")
ALERT_STATUS_PATH = Path("freshness_slo_status.json")
REPO_ENV = "GITHUB_REPOSITORY"
RUNBOOK_URL = "https://github.com/jpfelgueiras/btc-timesfm/blob/main/docs/FRESHNESS_SLO.md"
EVENT_LIMIT = 200
DEDUP_KEY_PREFIX = "slo-breach"


@dataclass(frozen=True)
class BreachSeverity:
    warning: str = "warning"
    critical: str = "critical"


@dataclass
class CheckResult:
    """Result of a single freshness check."""

    metric_name: str
    severity: str | None
    measured_hours: float | None
    target_hours: float
    message: str
    breached: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric_name": self.metric_name,
            "severity": self.severity,
            "measured_hours": self.measured_hours,
            "target_hours": self.target_hours,
            "message": self.message,
            "breached": self.breached,
        }


@dataclass
class AlertDedupEntry:
    key: str
    last_alerted_at: str
    alert_count: int


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "version": 1,
            "alerts": {},
            "last_run_at": None,
            "recoveries": [],
            "breaches": [],
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {
            "version": 1,
            "alerts": {},
            "last_run_at": None,
            "recoveries": [],
            "breaches": [],
        }
    if not isinstance(payload, dict):
        return {
            "version": 1,
            "alerts": {},
            "last_run_at": None,
            "recoveries": [],
            "breaches": [],
        }
    payload.setdefault("alerts", {})
    payload.setdefault("recoveries", [])
    payload.setdefault("breaches", [])
    return payload


def _save_state(state: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _record_event(
    event: str,
    *,
    metric_name: str,
    severity: str | None = None,
    message: str = "",
    measured_hours: float | None = None,
    details: dict[str, Any] | None = None,
    metrics_log_path: Path = METRICS_LOG_PATH,
) -> None:
    payload: dict[str, Any] = {
        "timestamp": _iso(_utc_now()),
        "event": event,
        "metric_name": metric_name,
        "severity": severity,
        "message": message,
    }
    if measured_hours is not None:
        payload["measured_hours"] = round(measured_hours, 2)
    if details:
        payload["details"] = details
    metrics_log_path.parent.mkdir(parents=True, exist_ok=True)
    with metrics_log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _dedup_key(metric_name: str, severity: str) -> str:
    return f"{DEDUP_KEY_PREFIX}:{metric_name}:{severity}"


def _should_alert(
    key: str,
    now: datetime,
    dedup_minutes: int,
    state: dict[str, Any],
) -> bool:
    entry = state["alerts"].get(key)
    if entry is None:
        return True
    if not isinstance(entry, dict):
        return True
    last_at_str = entry.get("last_alerted_at")
    if not isinstance(last_at_str, str):
        return True
    last_at = _parse_timestamp(last_at_str)
    if last_at is None:
        return True
    elapsed = (now - last_at).total_seconds() / 60.0
    return elapsed >= dedup_minutes


def _update_dedup(state: dict[str, Any], key: str, now: datetime) -> None:
    entry = state["alerts"].get(key)
    count = 0
    if isinstance(entry, dict):
        count = int(entry.get("alert_count", 0))
    state["alerts"][key] = {
        "last_alerted_at": _iso(now),
        "alert_count": count + 1,
    }


def _format_alert_message(result: CheckResult, severity: str) -> str:
    action = "Fix immediately" if severity == "critical" else "Investigate"
    return (
        f"[{severity.upper()}] Freshness SLO breached for '{result.metric_name}': "
        f"{result.message}. {action}. Runbook: {RUNBOOK_URL}"
    )


def _format_recovery_message(result: CheckResult) -> str:
    return (
        f"[RECOVERED] Freshness SLO metric '{result.metric_name}' is now healthy: {result.message}"
    )


def check_production_forecast(
    history_db_path: Path,
    config: FreshnessSLOConfig,
    *,
    now: datetime | None = None,
) -> CheckResult:
    """Check if the latest durable forecast is within SLO freshness."""
    metric = config.metric_by_name("production_forecast")
    if metric is None:
        return CheckResult(
            metric_name="production_forecast",
            severity=None,
            measured_hours=None,
            target_hours=0,
            message="production_forecast metric not configured",
        )

    checked_at = now or _utc_now()
    latest_origin = _latest_history_origin(history_db_path)

    if latest_origin is None:
        return CheckResult(
            metric_name="production_forecast",
            severity=BreachSeverity.critical,
            measured_hours=None,
            target_hours=metric.target_hours,
            message="No forecast history found — production forecast may have never run",
            breached=True,
        )

    age_hours = (checked_at - latest_origin).total_seconds() / 3600.0
    severity = _severity_for_age(age_hours, metric)
    breached = severity is not None
    msg = f"Latest forecast origin is {age_hours:.1f}h old (threshold: {metric.critical_tolerance_hours}h)"
    return CheckResult(
        metric_name="production_forecast",
        severity=severity,
        measured_hours=round(age_hours, 2),
        target_hours=metric.target_hours,
        message=msg,
        breached=breached,
    )


def check_site_update(
    site_data_path: Path,
    config: FreshnessSLOConfig,
    *,
    now: datetime | None = None,
) -> CheckResult:
    """Check if the public site generated-at is within SLO freshness."""
    metric = config.metric_by_name("site_update")
    if metric is None:
        return CheckResult(
            metric_name="site_update",
            severity=None,
            measured_hours=None,
            target_hours=0,
            message="site_update metric not configured",
        )

    checked_at = now or _utc_now()
    site_data = _load_site_data(site_data_path)

    if site_data is None:
        return CheckResult(
            metric_name="site_update",
            severity=BreachSeverity.critical,
            measured_hours=None,
            target_hours=metric.target_hours,
            message="Site data file not found or unreadable",
            breached=True,
        )

    generated_at = _parse_timestamp(site_data.get("generated_at"))
    if generated_at is None:
        return CheckResult(
            metric_name="site_update",
            severity=BreachSeverity.critical,
            measured_hours=None,
            target_hours=metric.target_hours,
            message="Site generated_at missing or unparseable",
            breached=True,
        )

    age_hours = (checked_at - generated_at).total_seconds() / 3600.0
    severity = _severity_for_age(age_hours, metric)
    breached = severity is not None
    msg = (
        f"Site generated-at is {age_hours:.1f}h old (threshold: {metric.critical_tolerance_hours}h)"
    )
    return CheckResult(
        metric_name="site_update",
        severity=severity,
        measured_hours=round(age_hours, 2),
        target_hours=metric.target_hours,
        message=msg,
        breached=breached,
    )


def check_history_backup(
    backup_assets_path: Path | None,
    config: FreshnessSLOConfig,
    *,
    now: datetime | None = None,
) -> CheckResult:
    """Check if the latest history backup is within SLO freshness."""
    metric = config.metric_by_name("history_backup")
    if metric is None:
        return CheckResult(
            metric_name="history_backup",
            severity=None,
            measured_hours=None,
            target_hours=0,
            message="history_backup metric not configured",
        )

    checked_at = now or _utc_now()
    if backup_assets_path is None or not backup_assets_path.exists():
        return CheckResult(
            metric_name="history_backup",
            severity=BreachSeverity.critical,
            measured_hours=None,
            target_hours=metric.target_hours,
            message="Backup assets file not found",
            breached=True,
        )

    try:
        raw = json.loads(backup_assets_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return CheckResult(
            metric_name="history_backup",
            severity=BreachSeverity.critical,
            measured_hours=None,
            target_hours=metric.target_hours,
            message="Backup assets file is not valid JSON",
            breached=True,
        )

    if not isinstance(raw, list):
        return CheckResult(
            metric_name="history_backup",
            severity=BreachSeverity.critical,
            measured_hours=None,
            target_hours=metric.target_hours,
            message="Backup assets file does not contain a list",
            breached=True,
        )

    latest_backup = _latest_backup_time(raw)
    if latest_backup is None:
        return CheckResult(
            metric_name="history_backup",
            severity=BreachSeverity.critical,
            measured_hours=None,
            target_hours=metric.target_hours,
            message="No backup assets found",
            breached=True,
        )

    age_hours = (checked_at - latest_backup).total_seconds() / 3600.0
    severity = _severity_for_age(age_hours, metric)
    breached = severity is not None
    msg = f"Latest backup is {age_hours:.1f}h old (threshold: {metric.critical_tolerance_hours}h)"
    return CheckResult(
        metric_name="history_backup",
        severity=severity,
        measured_hours=round(age_hours, 2),
        target_hours=metric.target_hours,
        message=msg,
        breached=breached,
    )


def check_scheduled_run(
    workflow_runs: list[dict[str, Any]],
    config: FreshnessSLOConfig,
    *,
    now: datetime | None = None,
) -> CheckResult:
    """Check if the last scheduled workflow run is within SLO freshness."""
    metric = config.metric_by_name("scheduled_run")
    if metric is None:
        return CheckResult(
            metric_name="scheduled_run",
            severity=None,
            measured_hours=None,
            target_hours=0,
            message="scheduled_run metric not configured",
        )

    checked_at = now or _utc_now()
    if not workflow_runs:
        return CheckResult(
            metric_name="scheduled_run",
            severity=BreachSeverity.critical,
            measured_hours=None,
            target_hours=metric.target_hours,
            message="No workflow runs found — scheduled dispatcher may have never executed",
            breached=True,
        )

    latest_run = _latest_scheduled_run(workflow_runs)
    if latest_run is None:
        return CheckResult(
            metric_name="scheduled_run",
            severity=BreachSeverity.critical,
            measured_hours=None,
            target_hours=metric.target_hours,
            message="No scheduled workflow runs found in recent history",
            breached=True,
        )

    run_time = _parse_timestamp(latest_run.get("updated_at") or latest_run.get("created_at"))
    if run_time is None:
        return CheckResult(
            metric_name="scheduled_run",
            severity=BreachSeverity.critical,
            measured_hours=None,
            target_hours=metric.target_hours,
            message="Latest scheduled workflow run has no parseable timestamp",
            breached=True,
        )

    age_hours = (checked_at - run_time).total_seconds() / 3600.0
    severity = _severity_for_age(age_hours, metric)
    breached = severity is not None
    run_status = latest_run.get("status", "unknown")
    run_conclusion = latest_run.get("conclusion", "")
    msg = (
        f"Last scheduled run was {age_hours:.1f}h ago "
        f"(status={run_status}, conclusion={run_conclusion}; "
        f"threshold: {metric.critical_tolerance_hours}h)"
    )
    return CheckResult(
        metric_name="scheduled_run",
        severity=severity,
        measured_hours=round(age_hours, 2),
        target_hours=metric.target_hours,
        message=msg,
        breached=breached,
    )


def run_all_checks(
    config: FreshnessSLOConfig,
    *,
    history_db_path: Path | None = None,
    site_data_path: Path | None = None,
    backup_assets_path: Path | None = None,
    workflow_runs: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
    state_path: Path = STATE_PATH,
    metrics_log_path: Path = METRICS_LOG_PATH,
    alert_status_path: Path = ALERT_STATUS_PATH,
) -> dict[str, Any]:
    """Run all freshness checks, record breaches/recoveries, emit alerts."""
    checked_at = now or _utc_now()
    state = _load_state(state_path)
    previous_breaches: set[str] = {
        str(b.get("metric_name", ""))
        for b in state.get("breaches", [])
        if b.get("breached") and isinstance(b.get("metric_name"), str)
    }

    results: list[CheckResult] = []
    if history_db_path is not None:
        results.append(check_production_forecast(history_db_path, config, now=checked_at))
    if site_data_path is not None:
        results.append(check_site_update(site_data_path, config, now=checked_at))
    if backup_assets_path is not None:
        results.append(check_history_backup(backup_assets_path, config, now=checked_at))
    if workflow_runs is not None:
        results.append(check_scheduled_run(workflow_runs, config, now=checked_at))

    alerts_emitted: list[dict[str, Any]] = []
    recoveries: list[dict[str, Any]] = []
    breaches_recorded: list[dict[str, Any]] = []

    current_breaches: set[str] = set()

    for result in results:
        event_type = "breach" if result.breached else "check_ok"
        _record_event(
            event_type,
            metric_name=result.metric_name,
            severity=result.severity,
            message=result.message,
            measured_hours=result.measured_hours,
            metrics_log_path=metrics_log_path,
        )

        if result.breached and result.severity:
            current_breaches.add(result.metric_name)
            breaches_recorded.append(result.as_dict())
            key = _dedup_key(result.metric_name, result.severity)
            if _should_alert(key, checked_at, config.alert_dedup_minutes, state):
                _update_dedup(state, key, checked_at)
                alert_message = _format_alert_message(result, result.severity)
                alerts_emitted.append(
                    {
                        "metric_name": result.metric_name,
                        "severity": result.severity,
                        "message": alert_message,
                        "measured_hours": result.measured_hours,
                        "runbook_url": RUNBOOK_URL,
                    }
                )
                _emit_alert(alert_message, result.severity, alert_status_path)

        if result.metric_name in previous_breaches and not result.breached:
            recoveries.append(
                {
                    "metric_name": result.metric_name,
                    "message": _format_recovery_message(result),
                    "recovered_at": _iso(checked_at),
                }
            )
            _record_event(
                "recovery",
                metric_name=result.metric_name,
                message=_format_recovery_message(result),
                metrics_log_path=metrics_log_path,
            )

    state["breaches"] = breaches_recorded
    state["recoveries"] = list(state.get("recoveries", []))[-50:] + recoveries
    del state["recoveries"][:-50]
    state["last_run_at"] = _iso(checked_at)

    stale_keys = [
        k
        for k in state["alerts"]
        if not any(
            _dedup_key(str(r.get("metric_name", "")), str(r.get("severity", ""))) == k
            for r in breaches_recorded
            if r.get("severity")
        )
    ]
    for k in stale_keys:
        del state["alerts"][k]

    _save_state(state, state_path)

    summary = {
        "checked_at": _iso(checked_at),
        "total_checks": len(results),
        "breaches": len(breaches_recorded),
        "recoveries": len(recoveries),
        "alerts_emitted": len(alerts_emitted),
        "results": [r.as_dict() for r in results],
        "alerts": alerts_emitted,
        "recovery_messages": recoveries,
    }

    status_path = alert_status_path
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return summary


def fetch_workflow_runs(
    repo: str | None = None, *, workflow: str = "forecast.yml"
) -> list[dict[str, Any]]:
    """Fetch recent GitHub Actions workflow runs via ``gh api``.

    Returns an empty list if the gh CLI is unavailable or returns an error,
    so callers never crash on missing tooling.
    """
    resolved_repo = repo or os.getenv(REPO_ENV)
    if not resolved_repo:
        return []
    try:
        completed = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{resolved_repo}/actions/workflows/{workflow}/runs",
                "--paginate",
                "--jq",
                ".workflow_runs[] | {id, status, conclusion, created_at, updated_at, run_started_at, event}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    if completed.returncode != 0:
        return []
    runs: list[dict[str, Any]] = []
    for line in completed.stdout.strip().splitlines():
        if not line.strip():
            continue
        try:
            runs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return runs


def emit_github_issue(
    title: str,
    body: str,
    *,
    repo: str | None = None,
    labels: tuple[str, ...] = ("freshness-slo", "alert"),
) -> str | None:
    """File a GitHub issue for an SLO breach via ``gh issue create``."""
    resolved_repo = repo or os.getenv(REPO_ENV)
    if not resolved_repo:
        return None
    cmd = [
        "gh",
        "issue",
        "create",
        "--repo",
        resolved_repo,
        "--title",
        title,
        "--body",
        body,
    ]
    for label in labels:
        cmd.extend(["--label", label])
    try:
        completed = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if completed.returncode != 0:
        return None
    url = completed.stdout.strip()
    return url if url.startswith("http") else None


def _emit_alert(
    message: str,
    severity: str,
    status_path: Path = ALERT_STATUS_PATH,
) -> None:
    """Write the alert to a machine-readable status file and stdout."""
    payload = {
        "timestamp": _iso(_utc_now()),
        "severity": severity,
        "message": message,
    }
    status_path.parent.mkdir(parents=True, exist_ok=True)
    with status_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    print(json.dumps(payload, sort_keys=True))


def _severity_for_age(age_hours: float, metric: FreshnessMetric) -> str | None:
    """Return severity level or None if within SLO."""
    if age_hours > metric.critical_tolerance_hours:
        return BreachSeverity.critical
    if age_hours > metric.warning_tolerance_hours:
        return BreachSeverity.warning
    return None


def _latest_history_origin(history_db_path: Path) -> datetime | None:
    """Extract the latest origin_at timestamp from the forecast history state."""
    if not history_db_path.exists():
        return None
    try:
        raw = json.loads(history_db_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    snapshots: list[dict[str, Any]] = []
    if isinstance(raw.get("forecasts"), list):
        snapshots = [item for item in raw["forecasts"] if isinstance(item, dict)]
    elif "predictions" in raw:
        snapshots = [raw]

    timestamps = [
        parsed
        for snapshot in snapshots
        if isinstance(snapshot, dict)
        and (parsed := _parse_timestamp(snapshot.get("latest_close_at"))) is not None
    ]
    return max(timestamps) if timestamps else None


def _load_site_data(site_data_path: Path) -> dict[str, Any] | None:
    """Load site data.json or index.html-generated-at metadata."""
    if not site_data_path.exists():
        return None
    try:
        return json.loads(site_data_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _latest_backup_time(assets: list[dict[str, Any]]) -> datetime | None:
    """Find the most recent backup creation timestamp from a list of assets."""
    from btc_timesfm.history.history_backup import BACKUP_PREFIX, BACKUP_SUFFIX

    timestamps: list[datetime] = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name", ""))
        if not (name.startswith(BACKUP_PREFIX) and name.endswith(BACKUP_SUFFIX)):
            continue
        parsed = _parse_timestamp(asset.get("created_at"))
        if parsed is not None:
            timestamps.append(parsed)
    return max(timestamps) if timestamps else None


def _latest_scheduled_run(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the most recent scheduled workflow run."""
    candidates: list[dict[str, Any]] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        if run.get("event") != "schedule":
            continue
        candidates.append(run)
    if not candidates:
        return None

    def _sort_key(run: dict[str, Any]) -> str:
        return str(run.get("updated_at") or run.get("created_at") or "")

    candidates.sort(key=_sort_key, reverse=True)
    return candidates[0] if candidates else None


def compute_weekly_slo_adherence(
    metrics_log_path: Path = METRICS_LOG_PATH,
    *,
    days: int = 7,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute per-metric and overall SLO adherence from recorded events.

    Reads the append-only JSONL metrics log and counts ``breach`` vs
    ``check_ok`` events within the rolling window. Adherence is the share of
    checks that did not breach the SLO.
    """
    checked_at = now or _utc_now()
    if days < 1:
        raise ValueError("days must be >= 1")
    cutoff = checked_at - timedelta(days=days)

    counts: dict[str, dict[str, int]] = {}
    if metrics_log_path.exists():
        with metrics_log_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                timestamp = _parse_timestamp(event.get("timestamp"))
                if timestamp is None or timestamp < cutoff:
                    continue
                metric_name = str(event.get("metric_name") or "")
                event_type = str(event.get("event") or "")
                if event_type not in {"breach", "check_ok"} or not metric_name:
                    continue
                entry = counts.setdefault(metric_name, {"checks": 0, "breaches": 0})
                entry["checks"] += 1
                if event_type == "breach":
                    entry["breaches"] += 1

    per_metric: dict[str, Any] = {}
    for name, item in sorted(counts.items()):
        checks = int(item["checks"])
        breaches = int(item["breaches"])
        adherence = 100.0 if checks == 0 else round((checks - breaches) / checks * 100.0, 2)
        per_metric[name] = {
            "checks": checks,
            "breaches": breaches,
            "adherence_pct": adherence,
        }

    total_checks = sum(int(item["checks"]) for item in counts.values())
    total_breaches = sum(int(item["breaches"]) for item in counts.values())
    overall_adherence = (
        100.0
        if total_checks == 0
        else round((total_checks - total_breaches) / total_checks * 100.0, 2)
    )
    return {
        "window_days": days,
        "computed_at": _iso(checked_at),
        "overall": {
            "checks": total_checks,
            "breaches": total_breaches,
            "adherence_pct": overall_adherence,
        },
        "per_metric": per_metric,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Freshness SLO watchdog — detect stale forecasts, site, backups and missed runs"
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_SLO_PATH)
    parser.add_argument("--state", type=Path, default=STATE_PATH)
    parser.add_argument("--metrics-log", type=Path, default=METRICS_LOG_PATH)
    parser.add_argument("--status", type=Path, default=ALERT_STATUS_PATH)
    parser.add_argument("--history-db", type=Path, default=None)
    parser.add_argument("--site-data", type=Path, default=None)
    parser.add_argument("--backup-assets", type=Path, default=None)
    parser.add_argument("--workflow", default="forecast.yml")
    parser.add_argument("--no-gh", action="store_true", help="Skip gh API calls")
    parser.add_argument(
        "--emit-issue", action="store_true", help="File GitHub issues for critical breaches"
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    now = _utc_now()

    workflow_runs: list[dict[str, Any]] | None = None
    if not args.no_gh:
        workflow_runs = fetch_workflow_runs(workflow=args.workflow)

    summary = run_all_checks(
        config,
        history_db_path=args.history_db,
        site_data_path=args.site_data,
        backup_assets_path=args.backup_assets,
        workflow_runs=workflow_runs,
        now=now,
        state_path=args.state,
        metrics_log_path=args.metrics_log,
        alert_status_path=args.status,
    )

    if args.emit_issue and summary.get("alerts_emitted", 0) > 0:
        critical = [a for a in summary.get("alerts", []) if a.get("severity") == "critical"]
        if critical:
            title = f"Freshness SLO breach: {len(critical)} critical metric(s) at {now.isoformat()}"
            body_lines = [
                "## Freshness SLO breach",
                "",
                f"Detected at: `{now.isoformat()}`",
                "",
            ]
            for alert in critical:
                body_lines.append(f"- **{alert['metric_name']}**: {alert['message']}")
            body_lines.extend(
                [
                    "",
                    f"Runbook: {RUNBOOK_URL}",
                    "",
                    "This issue was filed automatically by the freshness SLO watchdog.",
                ]
            )
            issue_url = emit_github_issue(title, "\n".join(body_lines))
            if issue_url:
                summary["github_issue_url"] = issue_url

    print(json.dumps(summary, indent=2, sort_keys=True))
    if summary.get("breaches", 0) > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
