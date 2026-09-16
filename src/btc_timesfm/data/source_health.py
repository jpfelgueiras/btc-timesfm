"""Freshness, completeness, revision, and disagreement health for external inputs."""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SOURCE_HEALTH_PATH = Path("source_health.json")
SOURCE_HEALTH_STATE_PATH = Path(".state/source_health_state.json")


@dataclass(frozen=True)
class SourceHealthConfig:
    max_optional_age_hours: float = 2.5
    max_missing_feature_ratio: float = 0.25
    quarantine_on_revision: bool = True
    quarantine_on_disagreement: bool = True

    @classmethod
    def from_env(cls) -> "SourceHealthConfig":
        return cls(
            max_optional_age_hours=_positive_float(
                "BTC_SOURCE_HEALTH_MAX_OPTIONAL_AGE_HOURS", cls.max_optional_age_hours
            ),
            max_missing_feature_ratio=_ratio(
                "BTC_SOURCE_HEALTH_MAX_MISSING_FEATURE_RATIO", cls.max_missing_feature_ratio
            ),
            quarantine_on_revision=_boolean(
                "BTC_SOURCE_HEALTH_QUARANTINE_ON_REVISION", cls.quarantine_on_revision
            ),
            quarantine_on_disagreement=_boolean(
                "BTC_SOURCE_HEALTH_QUARANTINE_ON_DISAGREEMENT",
                cls.quarantine_on_disagreement,
            ),
        )


def _positive_float(name: str, default: float) -> float:
    value = float(os.environ.get(name, default))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


def _ratio(name: str, default: float) -> float:
    value = float(os.environ.get(name, default))
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{name} must be between 0 and 1")
    return value


def _boolean(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    if value.lower() in {"1", "true", "yes"}:
        return True
    if value.lower() in {"0", "false", "no"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _as_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _load_state(path: Path) -> dict[str, str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return (
        value if isinstance(value, dict) and all(isinstance(v, str) for v in value.values()) else {}
    )


def persist_source_health(report: dict[str, Any], path: Path = SOURCE_HEALTH_PATH) -> None:
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def evaluate_source_health(
    snapshots: dict[str, dict[str, Any]],
    *,
    origin_at: datetime,
    market_comparison: dict[str, Any] | None = None,
    config: SourceHealthConfig | None = None,
    state_path: Path = SOURCE_HEALTH_STATE_PATH,
) -> dict[str, Any]:
    """Evaluate optional source snapshots and identify inputs unsafe for feature use."""
    cfg = config or SourceHealthConfig.from_env()
    origin = _as_utc(origin_at)
    prior = _load_state(state_path)
    updated = dict(prior)
    sources: dict[str, dict[str, Any]] = {}
    quarantined: list[str] = []

    for name, snapshot in snapshots.items():
        quality = snapshot.get("quality") if isinstance(snapshot.get("quality"), dict) else {}
        features = snapshot.get("features") if isinstance(snapshot.get("features"), dict) else {}
        missing = quality.get("missing_features", [])
        missing_count = len(missing) if isinstance(missing, list) else 0
        expected_count = len(features) + missing_count
        missing_ratio = missing_count / expected_count if expected_count else 1.0
        source_time = _parse_time(snapshot.get("captured_at") or snapshot.get("origin_at"))
        captured_age = (origin - source_time).total_seconds() / 3600.0 if source_time else None
        quality_ages = [
            float(value)
            for key, value in quality.items()
            if key.endswith("age_hours")
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        ]
        age_hours = max(quality_ages, default=captured_age)
        stale_markers = quality.get("stale_sources", [])
        stale = age_hours is None or age_hours > cfg.max_optional_age_hours or bool(stale_markers)
        incomplete = not bool(features) or missing_ratio > cfg.max_missing_feature_ratio
        key = f"{name}:{snapshot.get('origin_at') or origin.isoformat()}"
        digest = _digest(
            {
                "features": features,
                "quality": quality,
                "providers": snapshot.get("providers") or snapshot.get("provider"),
            }
        )
        revised = key in prior and prior[key] != digest
        updated[key] = digest
        reasons: list[str] = []
        if stale:
            reasons.append("stale")
        if incomplete:
            reasons.append("incomplete")
        if revised and cfg.quarantine_on_revision:
            reasons.append("revised")
        quarantined_input = bool(reasons)
        if quarantined_input:
            quarantined.append(name)
        sources[name] = {
            "status": snapshot.get("status", "unavailable"),
            "freshness_age_hours": round(age_hours, 6) if age_hours is not None else None,
            "missing_feature_count": missing_count,
            "expected_feature_count": expected_count,
            "missing_feature_ratio": round(missing_ratio, 6),
            "fallback_used": bool(snapshot.get("fallback_used", False)),
            "revised": revised,
            "quarantined": quarantined_input,
            "quarantine_reasons": reasons,
        }

    disagreement = bool(market_comparison and market_comparison.get("status") == "disagreement")
    if disagreement and cfg.quarantine_on_disagreement:
        quarantined.append("market_data")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "schema_version": 1,
        "origin_at": origin.isoformat(),
        "configuration": asdict(cfg),
        "sources": sources,
        "metrics": {
            "source_count": len(sources),
            "quarantined_source_count": len(set(quarantined)),
            "missing_feature_count": sum(
                item["missing_feature_count"] for item in sources.values()
            ),
            "fallback_source_count": sum(int(item["fallback_used"]) for item in sources.values()),
            "revised_source_count": sum(int(item["revised"]) for item in sources.values()),
            "disagreement_count": int(disagreement),
        },
        "market_comparison": market_comparison,
        "quarantined_sources": sorted(set(quarantined)),
    }
