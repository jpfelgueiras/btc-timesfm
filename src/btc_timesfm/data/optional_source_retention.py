"""Bounded, immutable retention and replay for optional-source snapshots."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


DEFAULT_RETENTION_PATH = Path(".state/optional_source_retention.json")


@dataclass(frozen=True)
class OptionalSourceRetentionConfig:
    retention_hours: float = 168.0
    max_records: int = 336

    @classmethod
    def from_env(cls) -> "OptionalSourceRetentionConfig":
        retention_hours = float(
            os.environ.get("BTC_OPTIONAL_SOURCE_RETENTION_HOURS", cls.retention_hours)
        )
        max_records = int(os.environ.get("BTC_OPTIONAL_SOURCE_RETENTION_MAX_RECORDS", cls.max_records))
        if retention_hours <= 0:
            raise ValueError("BTC_OPTIONAL_SOURCE_RETENTION_HOURS must be positive")
        if max_records <= 0:
            raise ValueError("BTC_OPTIONAL_SOURCE_RETENTION_MAX_RECORDS must be positive")
        return cls(retention_hours=retention_hours, max_records=max_records)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_origin(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _as_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _load(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    records = payload.get("records") if isinstance(payload, dict) else None
    return [record for record in records if isinstance(record, dict)] if isinstance(records, list) else []


def retain_optional_sources(
    snapshots: Mapping[str, Mapping[str, Any]],
    *,
    origin_at: datetime,
    path: Path = DEFAULT_RETENTION_PATH,
    config: OptionalSourceRetentionConfig | None = None,
) -> dict[str, Any]:
    """Append an immutable snapshot and prune records outside a bounded window."""
    cfg = config or OptionalSourceRetentionConfig.from_env()
    origin = _as_utc(origin_at)
    cutoff = origin - timedelta(hours=cfg.retention_hours)
    records = [
        record
        for record in _load(path)
        if (record_origin := _parse_origin(record.get("origin_at"))) is not None
        and cutoff <= record_origin <= origin
    ]
    record = {
        "origin_at": origin.isoformat(),
        "snapshots": copy.deepcopy(dict(snapshots)),
    }
    records = [item for item in records if item.get("origin_at") != record["origin_at"]]
    records.append(record)
    records.sort(key=lambda item: str(item["origin_at"]))
    records = records[-cfg.max_records :]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"schema_version": 1, "configuration": asdict(cfg), "records": records},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "status": "retained",
        "origin_at": origin.isoformat(),
        "retained_record_count": len(records),
        "retention_hours": cfg.retention_hours,
        "max_records": cfg.max_records,
        "path": str(path),
    }


def replay_optional_sources(
    origin_at: datetime,
    *,
    path: Path = DEFAULT_RETENTION_PATH,
    reconstruct: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Reconstruct retained inputs without writing retention or canonical history."""
    origin = _as_utc(origin_at).isoformat()
    record = next((item for item in _load(path) if item.get("origin_at") == origin), None)
    if record is None or not isinstance(record.get("snapshots"), dict):
        raise KeyError(f"No retained optional-source inputs for {origin}")
    snapshots = copy.deepcopy(record["snapshots"])
    features: dict[str, dict[str, Any]] = {}
    for source, snapshot in snapshots.items():
        if not isinstance(source, str) or not isinstance(snapshot, dict):
            continue
        reconstructed = reconstruct(source, snapshot) if reconstruct else snapshot.get("features", {})
        features[source] = dict(reconstructed) if isinstance(reconstructed, Mapping) else {}
    return {"origin_at": origin, "snapshots": snapshots, "features": features, "replayed": True}
