#!/usr/bin/env python3
"""Durable production health state, circuit breakers, and publication gates."""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


STATE_VERSION = 1
DEFAULT_STATE_PATH = Path(".state/pipeline_health.json")
DEFAULT_REPORT_PATH = Path("pipeline_health_report.json")
DEFAULT_SUMMARY_PATH = Path("pipeline_health_summary.md")
DEFAULT_VALIDATION_PATH = Path("market_data_validation.json")
DEFAULT_DRIFT_PATH = Path("drift_report.json")
DEFAULT_X_STATUS_PATH = Path("x_post_status.json")
WEBHOOK_ENV = "PIPELINE_HEALTH_WEBHOOK_URL"
EVENT_LIMIT = 100

STAGE_THRESHOLDS = {
    "market_data": 2,
    "forecast": 2,
    "history": 2,
    "x_post": 3,
}


@dataclass(frozen=True)
class HealthConfig:
    x_post_cooldown_minutes: int = 120

    @classmethod
    def from_env(cls) -> "HealthConfig":
        raw = os.getenv("BTC_X_CIRCUIT_COOLDOWN_MINUTES")
        if raw is None:
            return cls()
        value = int(raw)
        if value <= 0:
            raise ValueError("BTC_X_CIRCUIT_COOLDOWN_MINUTES must be a positive integer")
        return cls(x_post_cooldown_minutes=value)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def _initial_stage(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "health": "healthy",
        "circuit_state": "closed",
        "threshold": STAGE_THRESHOLDS[name],
        "consecutive_failures": 0,
        "last_success_at": None,
        "last_failure_at": None,
        "opened_at": None,
        "last_failure_class": None,
        "last_detail": None,
    }


def initial_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "updated_at": None,
        "stages": {name: _initial_stage(name) for name in STAGE_THRESHOLDS},
        "current_signals": {
            "market_data_status": "unknown",
            "drift_severity": "unknown",
            "forecast_outcome": "unknown",
            "history_outcome": "unknown",
            "x_status": "unknown",
        },
        "events": [],
    }


class PipelineHealth:
    def __init__(
        self,
        path: Path | str = DEFAULT_STATE_PATH,
        *,
        config: HealthConfig | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.config = config or HealthConfig.from_env()
        self.state = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return initial_state()
        payload = _read_json(self.path)
        if payload is None:
            raise RuntimeError(f"Pipeline health state {self.path} is not valid JSON")
        if payload.get("version") != STATE_VERSION:
            raise RuntimeError(
                f"Unsupported pipeline health state version {payload.get('version')!r}; "
                f"expected {STATE_VERSION}"
            )
        stages = payload.get("stages")
        signals = payload.get("current_signals")
        if not isinstance(stages, dict) or not isinstance(signals, dict):
            raise RuntimeError("Pipeline health state is missing required mappings")
        for name in STAGE_THRESHOLDS:
            if not isinstance(stages.get(name), dict):
                stages[name] = _initial_stage(name)
        if not isinstance(payload.get("events"), list):
            payload["events"] = []
        return payload

    def save(self) -> None:
        self.state["updated_at"] = _iso(_utc_now())
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def _stage(self, name: str) -> dict[str, Any]:
        if name not in STAGE_THRESHOLDS:
            raise ValueError(f"Unknown pipeline stage: {name}")
        return self.state["stages"][name]

    def _event(self, event: str, *, now: datetime, **fields: object) -> None:
        events: list[dict[str, Any]] = self.state["events"]
        events.append({"event": event, "timestamp": _iso(now), **fields})
        del events[:-EVENT_LIMIT]

    def record_success(
        self, stage: str, *, now: datetime | None = None, detail: str | None = None
    ) -> None:
        checked = now or _utc_now()
        item = self._stage(stage)
        previous_state = str(item.get("circuit_state", "closed"))
        previous_failures = int(item.get("consecutive_failures", 0))
        item.update(
            {
                "health": "healthy",
                "circuit_state": "closed",
                "consecutive_failures": 0,
                "last_success_at": _iso(checked),
                "opened_at": None,
                "last_failure_class": None,
                "last_detail": detail,
            }
        )
        self._event(
            "stage_success",
            now=checked,
            stage=stage,
            recovered=previous_state != "closed" or previous_failures > 0,
            detail=detail,
        )
        self.save()

    def record_failure(
        self,
        stage: str,
        *,
        failure_class: str,
        detail: str | None = None,
        now: datetime | None = None,
    ) -> None:
        checked = now or _utc_now()
        item = self._stage(stage)
        failures = int(item.get("consecutive_failures", 0)) + 1
        threshold = int(item.get("threshold", STAGE_THRESHOLDS[stage]))
        opened = failures >= threshold
        item.update(
            {
                "health": "open" if opened else "degraded",
                "circuit_state": "open" if opened else "closed",
                "consecutive_failures": failures,
                "last_failure_at": _iso(checked),
                "opened_at": _iso(checked) if opened else item.get("opened_at"),
                "last_failure_class": failure_class,
                "last_detail": detail,
            }
        )
        self._event(
            "stage_failure",
            now=checked,
            stage=stage,
            failure_class=failure_class,
            failures=failures,
            threshold=threshold,
            circuit_open=opened,
            detail=detail,
        )
        self.save()

    def observe_forecast(
        self,
        *,
        outcome: str,
        validation_path: Path = DEFAULT_VALIDATION_PATH,
        drift_path: Path = DEFAULT_DRIFT_PATH,
        now: datetime | None = None,
    ) -> None:
        checked = now or _utc_now()
        signals = self.state["current_signals"]
        normalized_outcome = "success" if outcome == "success" else "failure"
        signals["forecast_outcome"] = normalized_outcome
        if normalized_outcome == "success":
            self.record_success("forecast", now=checked)
        else:
            self.record_failure(
                "forecast",
                failure_class="execution",
                detail=f"workflow step outcome: {outcome or 'unknown'}",
                now=checked,
            )

        validation = _read_json(validation_path)
        if validation is not None:
            status = str(validation.get("status", "unknown"))
            signals["market_data_status"] = "healthy" if status == "ok" else "unhealthy"
            errors = validation.get("errors")
            error_codes = []
            if isinstance(errors, list):
                error_codes = [
                    str(item.get("code"))
                    for item in errors
                    if isinstance(item, dict) and item.get("code")
                ]
            if status == "ok":
                self.record_success("market_data", now=checked)
            else:
                self.record_failure(
                    "market_data",
                    failure_class="validation",
                    detail=",".join(error_codes) or status,
                    now=checked,
                )
        elif normalized_outcome == "failure":
            signals["market_data_status"] = "unknown"

        drift = _read_json(drift_path)
        if drift is not None:
            severity = str(drift.get("severity", "unknown"))
            signals["drift_severity"] = severity
        elif normalized_outcome == "failure":
            signals["drift_severity"] = "unknown"
        self.save()

    def observe_history(self, *, outcome: str, now: datetime | None = None) -> None:
        checked = now or _utc_now()
        normalized = "success" if outcome == "success" else "failure"
        self.state["current_signals"]["history_outcome"] = normalized
        if normalized == "success":
            self.record_success("history", now=checked)
        else:
            self.record_failure(
                "history",
                failure_class="persistence",
                detail=f"workflow step outcome: {outcome or 'unknown'}",
                now=checked,
            )
        self.save()

    def observe_x_status(
        self,
        *,
        status_path: Path = DEFAULT_X_STATUS_PATH,
        phase: str,
        now: datetime | None = None,
    ) -> None:
        checked = now or _utc_now()
        payload = _read_json(status_path)
        if payload is None:
            return
        status = str(payload.get("status", "unknown"))
        self.state["current_signals"]["x_status"] = status
        if status in {"prepared", "posted", "duplicate_skipped", "duplicate_locked"}:
            self.record_success("x_post", now=checked, detail=f"{phase}:{status}")
        elif status in {"preflight_failed", "failed"}:
            self.record_failure(
                "x_post",
                failure_class=str(payload.get("failure_class") or "provider_error"),
                detail=f"{phase}:{status}",
                now=checked,
            )
        self.save()

    def publication_gate(
        self,
        *,
        now: datetime | None = None,
        ignore_stages: Iterable[str] = (),
    ) -> dict[str, Any]:
        checked = now or _utc_now()
        ignored = set(ignore_stages)
        blockers: list[str] = []
        signals = self.state["current_signals"]

        if signals.get("market_data_status") == "unhealthy":
            blockers.append("current_market_data_unhealthy")
        if signals.get("forecast_outcome") == "failure":
            blockers.append("current_forecast_failed")
        if signals.get("drift_severity") == "severe":
            blockers.append("severe_model_or_feature_drift")
        if "history" not in ignored and signals.get("history_outcome") == "failure":
            blockers.append("current_history_persistence_failed")

        for stage in ("market_data", "forecast", "history"):
            if stage in ignored:
                continue
            if self._stage(stage).get("circuit_state") == "open":
                blockers.append(f"{stage}_circuit_open")

        x_stage = self._stage("x_post")
        x_probe = False
        if "x_post" not in ignored and x_stage.get("circuit_state") == "open":
            opened_at = _parse_time(x_stage.get("opened_at"))
            cooldown = timedelta(minutes=self.config.x_post_cooldown_minutes)
            if opened_at is not None and checked >= opened_at + cooldown:
                x_stage["circuit_state"] = "half_open"
                x_stage["health"] = "degraded"
                x_probe = True
                self._event(
                    "circuit_half_open",
                    now=checked,
                    stage="x_post",
                    cooldown_minutes=self.config.x_post_cooldown_minutes,
                )
            else:
                blockers.append("x_post_circuit_open")
        elif "x_post" not in ignored and x_stage.get("circuit_state") == "half_open":
            blockers.append("x_post_half_open_probe_in_progress")

        allowed = not blockers
        report = self.report(
            publication_allowed=allowed,
            blockers=blockers,
            x_half_open_probe=x_probe,
            checked_at=checked,
        )
        self.save()
        return report

    def report(
        self,
        *,
        publication_allowed: bool | None = None,
        blockers: list[str] | None = None,
        x_half_open_probe: bool = False,
        checked_at: datetime | None = None,
    ) -> dict[str, Any]:
        stages = self.state["stages"]
        overall = "healthy"
        if any(item.get("circuit_state") == "open" for item in stages.values()):
            overall = "open"
        elif any(
            item.get("health") == "degraded" or item.get("circuit_state") == "half_open"
            for item in stages.values()
        ):
            overall = "degraded"
        return {
            "schema_version": STATE_VERSION,
            "checked_at": _iso(checked_at or _utc_now()),
            "overall_health": overall,
            "publication_allowed": publication_allowed,
            "blockers": blockers or [],
            "x_half_open_probe": x_half_open_probe,
            "configuration": {
                "stage_failure_thresholds": STAGE_THRESHOLDS,
                "x_post_cooldown_minutes": self.config.x_post_cooldown_minutes,
            },
            "current_signals": dict(self.state["current_signals"]),
            "stages": {name: dict(item) for name, item in stages.items()},
            "recent_events": list(self.state["events"][-20:]),
        }


def render_summary(report: dict[str, Any]) -> str:
    allowed = report.get("publication_allowed")
    allowed_text = "not evaluated" if allowed is None else ("yes" if allowed else "no")
    lines = [
        "## Pipeline health",
        "",
        f"- Overall health: **{str(report.get('overall_health', 'unknown')).upper()}**",
        f"- Publication allowed: **{allowed_text}**",
    ]
    blockers = report.get("blockers", [])
    if isinstance(blockers, list) and blockers:
        lines.append(f"- Blockers: **{', '.join(str(item) for item in blockers)}**")
    lines.extend(
        [
            "",
            "| Stage | Health | Circuit | Consecutive failures | Threshold |",
            "| --- | --- | --- | ---: | ---: |",
        ]
    )
    stages = report.get("stages", {})
    if isinstance(stages, dict):
        for name in STAGE_THRESHOLDS:
            item = stages.get(name, {})
            if not isinstance(item, dict):
                continue
            lines.append(
                f"| `{name}` | {item.get('health')} | {item.get('circuit_state')} | "
                f"{int(item.get('consecutive_failures', 0))} | {int(item.get('threshold', 0))} |"
            )
    return "\n".join(lines) + "\n"


def persist_report(
    report: dict[str, Any],
    *,
    report_path: Path = DEFAULT_REPORT_PATH,
    summary_path: Path = DEFAULT_SUMMARY_PATH,
) -> None:
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary_path.write_text(render_summary(report), encoding="utf-8")


def _set_output(name: str, value: str) -> None:
    path = os.getenv("GITHUB_OUTPUT")
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def notify_webhook(report: dict[str, Any]) -> bool:
    url = os.getenv(WEBHOOK_ENV)
    if not url:
        return False
    if report.get("overall_health") == "healthy" and report.get("publication_allowed") is not False:
        return False
    body = json.dumps(
        {
            "event": "btc_timesfm_pipeline_health",
            "checked_at": report.get("checked_at"),
            "overall_health": report.get("overall_health"),
            "publication_allowed": report.get("publication_allowed"),
            "blockers": report.get("blockers", []),
            "current_signals": report.get("current_signals", {}),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        if int(response.status) >= 300:
            raise RuntimeError(f"Pipeline-health webhook returned HTTP {response.status}")
    return True


def _health(args: argparse.Namespace) -> PipelineHealth:
    return PipelineHealth(args.state, config=HealthConfig.from_env())


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage durable forecast-pipeline health state")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)

    observe_forecast = subparsers.add_parser("observe-forecast")
    observe_forecast.add_argument("--outcome", required=True)
    observe_forecast.add_argument("--validation", type=Path, default=DEFAULT_VALIDATION_PATH)
    observe_forecast.add_argument("--drift", type=Path, default=DEFAULT_DRIFT_PATH)

    observe_history = subparsers.add_parser("observe-history")
    observe_history.add_argument("--outcome", required=True)

    observe_x = subparsers.add_parser("observe-x")
    observe_x.add_argument("--status", type=Path, default=DEFAULT_X_STATUS_PATH)
    observe_x.add_argument("--phase", choices=("preflight", "publish"), required=True)

    gate = subparsers.add_parser("gate")
    gate.add_argument("--ignore-stage", action="append", default=[])
    gate.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    gate.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY_PATH)

    report_cmd = subparsers.add_parser("report")
    report_cmd.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    report_cmd.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY_PATH)

    notify = subparsers.add_parser("notify")
    notify.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)

    args = parser.parse_args()
    health = _health(args)

    if args.command == "observe-forecast":
        health.observe_forecast(
            outcome=args.outcome,
            validation_path=args.validation,
            drift_path=args.drift,
        )
        persist_report(health.report())
    elif args.command == "observe-history":
        health.observe_history(outcome=args.outcome)
        persist_report(health.report())
    elif args.command == "observe-x":
        health.observe_x_status(status_path=args.status, phase=args.phase)
        persist_report(health.report())
    elif args.command == "gate":
        result = health.publication_gate(ignore_stages=args.ignore_stage)
        persist_report(result, report_path=args.report, summary_path=args.summary)
        _set_output("allow", "true" if result["publication_allowed"] else "false")
        _set_output("blockers", ",".join(result["blockers"]))
    elif args.command == "report":
        persist_report(health.report(), report_path=args.report, summary_path=args.summary)
    elif args.command == "notify":
        report = _read_json(args.report)
        if report is not None:
            notify_webhook(report)


if __name__ == "__main__":
    main()
