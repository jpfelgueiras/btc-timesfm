#!/usr/bin/env python3
"""Machine-readable SLO configuration for forecast freshness monitoring.

Defines per-metric targets and tolerances used by the freshness watchdog to
detect stale forecasts, stale site, missed backups and missed scheduled runs.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SLO_VERSION = 1
DEFAULT_SLO_PATH = Path("freshness_slo.json")


@dataclass(frozen=True)
class FreshnessMetric:
    """One freshness SLO metric with target and tolerance."""

    name: str
    description: str
    target_hours: float
    warning_tolerance_hours: float
    critical_tolerance_hours: float

    def __post_init__(self) -> None:
        if self.target_hours <= 0:
            raise ValueError(f"target_hours must be positive for {self.name}")
        if self.warning_tolerance_hours < 0:
            raise ValueError(f"warning_tolerance_hours must be >= 0 for {self.name}")
        if self.critical_tolerance_hours < self.warning_tolerance_hours:
            raise ValueError(
                f"critical_tolerance_hours must be >= warning_tolerance_hours for {self.name}"
            )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FreshnessSLOConfig:
    """Complete SLO configuration for forecast freshness monitoring."""

    schema_version: int = SLO_VERSION
    metrics: tuple[FreshnessMetric, ...] = field(default_factory=tuple)
    backup_max_age_hours: float = 24.0
    scheduled_run_max_gap_hours: float = 2.0
    alert_dedup_minutes: int = 60

    def __post_init__(self) -> None:
        if self.schema_version != SLO_VERSION:
            raise ValueError(f"Unsupported SLO schema version: {self.schema_version}")
        if self.backup_max_age_hours <= 0:
            raise ValueError("backup_max_age_hours must be positive")
        if self.scheduled_run_max_gap_hours <= 0:
            raise ValueError("scheduled_run_max_gap_hours must be positive")
        if self.alert_dedup_minutes <= 0:
            raise ValueError("alert_dedup_minutes must be positive")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "metrics": [m.as_dict() for m in self.metrics],
            "backup_max_age_hours": self.backup_max_age_hours,
            "scheduled_run_max_gap_hours": self.scheduled_run_max_gap_hours,
            "alert_dedup_minutes": self.alert_dedup_minutes,
        }

    def metric_by_name(self, name: str) -> FreshnessMetric | None:
        for metric in self.metrics:
            if metric.name == name:
                return metric
        return None

    def all_metric_names(self) -> tuple[str, ...]:
        return tuple(m.name for m in self.metrics)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_slo_config() -> FreshnessSLOConfig:
    """Production SLO configuration with sensible defaults."""
    return FreshnessSLOConfig(
        metrics=(
            FreshnessMetric(
                name="production_forecast",
                description="Forecast should be within 3h of its scheduled hour",
                target_hours=1.0,
                warning_tolerance_hours=2.0,
                critical_tolerance_hours=3.0,
            ),
            FreshnessMetric(
                name="site_update",
                description="Public site generated-at should be within 2h of completed forecast",
                target_hours=1.0,
                warning_tolerance_hours=1.5,
                critical_tolerance_hours=2.0,
            ),
            FreshnessMetric(
                name="history_backup",
                description="Backup should be newer than 24h",
                target_hours=12.0,
                warning_tolerance_hours=20.0,
                critical_tolerance_hours=24.0,
            ),
            FreshnessMetric(
                name="scheduled_run",
                description="Scheduled workflow should run every ~1h",
                target_hours=1.0,
                warning_tolerance_hours=1.5,
                critical_tolerance_hours=2.0,
            ),
        ),
        backup_max_age_hours=24.0,
        scheduled_run_max_gap_hours=2.0,
        alert_dedup_minutes=60,
    )


def load_config(path: Path | str = DEFAULT_SLO_PATH) -> FreshnessSLOConfig:
    """Load SLO config from a JSON file, falling back to defaults."""
    config_path = Path(path)
    if not config_path.exists():
        return default_slo_config()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default_slo_config()
    if not isinstance(raw, dict):
        return default_slo_config()
    if raw.get("schema_version") != SLO_VERSION:
        return default_slo_config()
    metrics: list[FreshnessMetric] = []
    for item in raw.get("metrics", []):
        if not isinstance(item, dict):
            continue
        try:
            metrics.append(
                FreshnessMetric(
                    name=str(item["name"]),
                    description=str(item.get("description", "")),
                    target_hours=float(item["target_hours"]),
                    warning_tolerance_hours=float(item["warning_tolerance_hours"]),
                    critical_tolerance_hours=float(item["critical_tolerance_hours"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return FreshnessSLOConfig(
        metrics=tuple(metrics),
        backup_max_age_hours=float(raw.get("backup_max_age_hours", 24.0)),
        scheduled_run_max_gap_hours=float(raw.get("scheduled_run_max_gap_hours", 2.0)),
        alert_dedup_minutes=int(raw.get("alert_dedup_minutes", 60)),
    )


def save_config(config: FreshnessSLOConfig, path: Path | str = DEFAULT_SLO_PATH) -> None:
    """Persist an SLO configuration to JSON."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(config.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
