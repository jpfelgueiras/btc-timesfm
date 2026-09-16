#!/usr/bin/env python3
"""Versioned forecast-service SLO evaluation and reliability dashboard rendering."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


SLO_SCHEMA_VERSION = 1
DEFAULT_CONFIG_PATH = Path("service_slo.json")
DEFAULT_EVENTS_PATH = Path("forecast_observability.jsonl")
DEFAULT_REPORT_PATH = Path("service_slo_dashboard.json")
DEFAULT_MARKDOWN_PATH = Path("service_slo_dashboard.md")
DOMAINS = ("data", "model", "storage", "api", "publication")
SUCCESS_STATUSES = frozenset({"success", "healthy", "ok"})
FAILURE_STATUSES = frozenset({"failed", "error", "unhealthy"})


@dataclass(frozen=True)
class ServiceSLO:
    name: str
    domain: str
    description: str
    target_percent: float
    window_days: int
    min_events: int
    review_cadence_days: int
    threshold_basis: str

    def __post_init__(self) -> None:
        if self.domain not in DOMAINS:
            raise ValueError(f"Unsupported SLO domain {self.domain!r}")
        if not 0 < self.target_percent < 100:
            raise ValueError(f"target_percent must be between 0 and 100 for {self.name}")
        if self.window_days <= 0 or self.min_events <= 0 or self.review_cadence_days <= 0:
            raise ValueError(
                f"window_days, min_events and review_cadence_days must be positive for {self.name}"
            )
        if not self.threshold_basis:
            raise ValueError(f"threshold_basis is required for {self.name}")


@dataclass(frozen=True)
class ServiceSLOConfig:
    schema_version: int
    service: str
    objectives: tuple[ServiceSLO, ...]

    def __post_init__(self) -> None:
        if self.schema_version != SLO_SCHEMA_VERSION:
            raise ValueError(f"Unsupported service SLO schema version: {self.schema_version}")
        names = [objective.name for objective in self.objectives]
        if len(names) != len(set(names)):
            raise ValueError("SLO objective names must be unique")
        if set(objective.domain for objective in self.objectives) != set(DOMAINS):
            raise ValueError(
                "Service SLO config must cover data, model, storage, api, and publication"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "service": self.service,
            "objectives": [asdict(item) for item in self.objectives],
        }


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> ServiceSLOConfig:
    """Load and validate the versioned service SLO source of truth."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Service SLO config must be a JSON object")
    objectives = raw.get("objectives")
    if not isinstance(objectives, list):
        raise ValueError("Service SLO config requires an objectives list")
    try:
        parsed = tuple(ServiceSLO(**item) for item in objectives if isinstance(item, dict))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid service SLO objective: {exc}") from exc
    return ServiceSLOConfig(
        schema_version=int(raw.get("schema_version", 0)),
        service=str(raw.get("service", "")),
        objectives=parsed,
    )


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_events(path: Path | str) -> list[dict[str, Any]]:
    """Load newline-delimited normalized outcome events, ignoring malformed rows."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def evaluate_objective(
    objective: ServiceSLO, events: Iterable[dict[str, Any]], *, now: datetime | None = None
) -> dict[str, Any]:
    """Evaluate eligible domain outcomes and account for the rolling error budget."""
    evaluated_at = now or datetime.now(timezone.utc)
    if evaluated_at.tzinfo is None:
        evaluated_at = evaluated_at.replace(tzinfo=timezone.utc)
    cutoff = evaluated_at.astimezone(timezone.utc) - timedelta(days=objective.window_days)
    eligible: list[dict[str, Any]] = []
    for event in events:
        if event.get("domain") != objective.domain:
            continue
        timestamp = _parse_time(event.get("timestamp"))
        status = str(event.get("status", "")).lower()
        if (
            timestamp is not None
            and timestamp >= cutoff
            and status in SUCCESS_STATUSES | FAILURE_STATUSES
        ):
            eligible.append(event)
    total = len(eligible)
    failures = sum(str(event["status"]).lower() in FAILURE_STATUSES for event in eligible)
    successes = total - failures
    allowed_failures = total * (100.0 - objective.target_percent) / 100.0
    remaining = allowed_failures - failures
    achieved = 100.0 if total == 0 else successes / total * 100.0
    return {
        "name": objective.name,
        "domain": objective.domain,
        "description": objective.description,
        "target_percent": objective.target_percent,
        "window_days": objective.window_days,
        "min_events": objective.min_events,
        "review_cadence_days": objective.review_cadence_days,
        "threshold_basis": objective.threshold_basis,
        "events": total,
        "successes": successes,
        "failures": failures,
        "availability_percent": round(achieved, 3),
        "allowed_failures": round(allowed_failures, 3),
        "remaining_error_budget": round(remaining, 3),
        "error_budget_consumed_percent": round(
            0.0 if allowed_failures == 0 else failures / allowed_failures * 100.0, 3
        ),
        "status": "insufficient_data"
        if total < objective.min_events
        else ("met" if failures <= allowed_failures else "breached"),
    }


def build_dashboard(
    config: ServiceSLOConfig, events: Iterable[dict[str, Any]], *, now: datetime | None = None
) -> dict[str, Any]:
    """Build a machine-readable reliability dashboard, grouped by service domain."""
    event_list = list(events)
    objectives = [evaluate_objective(item, event_list, now=now) for item in config.objectives]
    return {
        "schema_version": SLO_SCHEMA_VERSION,
        "service": config.service,
        "generated_at": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
        "domains": {
            domain: [item for item in objectives if item["domain"] == domain] for domain in DOMAINS
        },
        "summary": {
            "objectives": len(objectives),
            "met": sum(item["status"] == "met" for item in objectives),
            "breached": sum(item["status"] == "breached" for item in objectives),
            "insufficient_data": sum(item["status"] == "insufficient_data" for item in objectives),
        },
    }


def dashboard_markdown(dashboard: dict[str, Any]) -> str:
    """Render a human-readable dashboard without binding to a dashboard provider."""
    lines = [
        "# Forecast service reliability dashboard",
        "",
        "| Domain | SLO | Status | Availability | Budget remaining |",
        "| --- | --- | --- | ---: | ---: |",
    ]
    for domain, objectives in dashboard["domains"].items():
        for objective in objectives:
            lines.append(
                f"| {domain} | `{objective['name']}` | {objective['status']} | {objective['availability_percent']:.3f}% | {objective['remaining_error_budget']:.3f} events |"
            )
    lines.extend(
        [
            "",
            "Thresholds are evaluated from timestamped normalized outcome events over each stated rolling window. Review targets and threshold basis at the configured cadence.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate forecast-service SLOs and render reliability dashboards"
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS_PATH)
    parser.add_argument("--json", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN_PATH)
    args = parser.parse_args()
    dashboard = build_dashboard(load_config(args.config), load_events(args.events))
    args.json.write_text(json.dumps(dashboard, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.markdown.write_text(dashboard_markdown(dashboard), encoding="utf-8")


if __name__ == "__main__":
    main()
