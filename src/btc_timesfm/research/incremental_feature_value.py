"""Point-in-time evidence gate for a small set of incremental feature families.

This module inventories supplied rows; it does not fetch data, infer forecasts,
or claim feature value. Feature observations need explicit capture and vintage
timestamps no later than the forecast origin to count as usable.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from btc_timesfm.forecasting.feature_registry import FEATURE_REGISTRY

CANDIDATE_GROUPS = {
    "volume_transform": frozenset({"volume_zscore_7d"}),
    "ohlc_shape_volatility": frozenset(
        {"range_24h_avg_pct", "volatility_6h_pct", "volatility_24h_pct", "volatility_7d_pct"}
    ),
    "calendar": frozenset(
        {"hour_utc", "weekday_utc", "hour_sin", "hour_cos", "weekday_sin", "weekday_cos"}
    ),
    "external_eth_derivatives": frozenset(
        name
        for name in FEATURE_REGISTRY
        if name.startswith(("cross_eth_", "derivatives_funding", "derivatives_open_interest"))
    ),
}


def _point_in_time(value: Any, origin: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        observed_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        origin_at = datetime.fromisoformat(origin.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (
        observed_at.tzinfo is not None and origin_at.tzinfo is not None and observed_at <= origin_at
    )


def _available_features(row: Mapping[str, Any]) -> set[str]:
    origin = row.get("origin_at")
    features = row.get("features")
    if not isinstance(origin, str) or not isinstance(features, Mapping):
        return set()
    captured = row.get("feature_capture_times", {})
    vintages = row.get("feature_vintages", {})
    if not isinstance(captured, Mapping) or not isinstance(vintages, Mapping):
        return set()
    return {
        name
        for name, value in features.items()
        if name in FEATURE_REGISTRY
        and value is not None
        and _point_in_time(captured.get(name), origin)
        and _point_in_time(vintages.get(name), origin)
    }


def audit_feature_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize missingness and point-in-time eligibility by bounded group."""
    availability = [_available_features(row) for row in rows]
    all_origins = len(rows)
    groups: dict[str, Any] = {}
    for group, names in CANDIDATE_GROUPS.items():
        counts = {
            name: sum(name in available for available in availability) for name in sorted(names)
        }
        common = sum(names <= available for available in availability)
        groups[group] = {
            "registered_features": sorted(names),
            "available_by_feature": counts,
            "missing_by_feature": {name: all_origins - count for name, count in counts.items()},
            "all_origin_count": all_origins,
            "common_available_origin_count": common,
            "common_available_origin_indices": [
                index for index, available in enumerate(availability) if names <= available
            ],
            "status": "inventory_only" if all_origins else "blocked_no_rows",
        }
    return {
        "status": "blocked",
        "source": "caller_supplied_origin_rows",
        "origin_count": all_origins,
        "feature_sources": {
            name: {
                "source": definition.source,
                "source_schema_version": definition.source_schema_version,
                "units": "not_declared_in_feature_registry",
            }
            for name, definition in sorted(FEATURE_REGISTRY.items())
        },
        "groups": groups,
        "external_data_status": "blocked_no_point_in_time_external_corpus",
        "performance_claim": None,
        "covariate_api": {"status": "blocked_unsupported_unverified"},
    }


def baseline_or_candidate(baseline: Any, optional_candidate: Any) -> Any:
    """Preserve exact baseline output when an optional input is absent."""
    return baseline if optional_candidate is None else optional_candidate
