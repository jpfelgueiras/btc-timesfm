"""Bounded, immutable retention and replay for optional-source snapshots."""

from __future__ import annotations

import copy
import hashlib
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
        max_records = int(
            os.environ.get("BTC_OPTIONAL_SOURCE_RETENTION_MAX_RECORDS", cls.max_records)
        )
        if retention_hours <= 0:
            raise ValueError("BTC_OPTIONAL_SOURCE_RETENTION_HOURS must be positive")
        if max_records <= 0:
            raise ValueError("BTC_OPTIONAL_SOURCE_RETENTION_MAX_RECORDS must be positive")
        return cls(retention_hours=retention_hours, max_records=max_records)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _load(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict)]


def _identifier(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _normalized_metadata(
    source: str,
    snapshot: Mapping[str, Any],
    supplied: Mapping[str, Any],
    origin: datetime,
    observed: datetime,
) -> dict[str, Any]:
    metadata = copy.deepcopy(dict(supplied))
    metadata.setdefault("event_time", snapshot.get("event_time") or snapshot.get("origin_at"))
    metadata.setdefault("published_time", snapshot.get("published_at"))
    metadata.setdefault("observed_time", observed.isoformat())
    metadata.setdefault("captured_time", snapshot.get("captured_at") or observed.isoformat())
    metadata.setdefault("vintage", snapshot.get("vintage"))
    metadata.setdefault("model_use_cutoff", origin.isoformat())
    metadata.setdefault("status", snapshot.get("status"))
    metadata.setdefault("available", snapshot.get("available", False))
    metadata.setdefault("quality", snapshot.get("quality", {}))
    metadata["first_seen_id"] = _identifier({"source": source, "snapshot": snapshot})
    return metadata


def retain_optional_sources(
    snapshots: Mapping[str, Mapping[str, Any]],
    *,
    origin_at: datetime,
    observed_at: datetime | None = None,
    metadata_per_source: Mapping[str, Mapping[str, Any]] | None = None,
    path: Path = DEFAULT_RETENTION_PATH,
    config: OptionalSourceRetentionConfig | None = None,
) -> dict[str, Any]:
    """Retain first observations and append distinct same-origin revisions."""
    cfg = config or OptionalSourceRetentionConfig.from_env()
    origin = _as_utc(origin_at)
    observed = _as_utc(observed_at or datetime.now(timezone.utc))
    cutoff = observed - timedelta(hours=cfg.retention_hours)
    records = [
        record
        for record in _load(path)
        if (seen := _parse_time(record.get("observed_at") or record.get("origin_at"))) is not None
        and cutoff <= seen <= observed
    ]
    supplied = metadata_per_source or {}
    retained_snapshots = {
        source: {
            "data": copy.deepcopy(dict(snapshot)),
            "metadata": _normalized_metadata(
                source, snapshot, supplied.get(source, {}), origin, observed
            ),
        }
        for source, snapshot in snapshots.items()
    }
    revision_id = _identifier({"origin_at": origin.isoformat(), "snapshots": retained_snapshots})
    duplicate = any(record.get("revision_id") == revision_id for record in records)
    if not duplicate:
        origin_revisions = sum(record.get("origin_at") == origin.isoformat() for record in records)
        records.append(
            {
                "origin_at": origin.isoformat(),
                "observed_at": observed.isoformat(),
                "revision": origin_revisions + 1,
                "revision_id": revision_id,
                "snapshots": retained_snapshots,
            }
        )
    records.sort(
        key=lambda item: (
            str(item.get("observed_at", "")),
            str(item.get("origin_at", "")),
            int(item.get("revision", 1)),
        )
    )
    records = records[-cfg.max_records :]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"schema_version": 2, "configuration": asdict(cfg), "records": records},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "status": "duplicate" if duplicate else "retained",
        "origin_at": origin.isoformat(),
        "observed_at": observed.isoformat(),
        "revision_id": revision_id,
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
    """Replay the first-observed revision for an origin without mutating storage."""
    origin = _as_utc(origin_at).isoformat()
    matches = [record for record in _load(path) if record.get("origin_at") == origin]
    if not matches:
        raise KeyError(f"No retained optional-source inputs for {origin}")
    record = min(matches, key=lambda item: (str(item.get("observed_at", "")), int(item.get("revision", 1))))
    snapshots_value = record.get("snapshots")
    if not isinstance(snapshots_value, dict):
        raise KeyError(f"No retained optional-source inputs for {origin}")
    snapshots = copy.deepcopy(snapshots_value)
    features: dict[str, dict[str, Any]] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for source, retained in snapshots.items():
        if not isinstance(source, str) or not isinstance(retained, dict):
            continue
        data_value = retained.get("data", retained)
        data = data_value if isinstance(data_value, dict) else {}
        metadata_value = retained.get("metadata", {})
        reconstructed = reconstruct(source, data) if reconstruct else data.get("features", {})
        features[source] = dict(reconstructed) if isinstance(reconstructed, Mapping) else {}
        metadata[source] = dict(metadata_value) if isinstance(metadata_value, Mapping) else {}
    return {
        "origin_at": origin,
        "observed_at": record.get("observed_at"),
        "revision": record.get("revision", 1),
        "revision_id": record.get("revision_id"),
        "snapshots": snapshots,
        "features": features,
        "metadata": metadata,
        "replayed": True,
    }
