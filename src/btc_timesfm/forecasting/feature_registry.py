"""Versioned feature definitions and manifest lineage contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


FEATURE_REGISTRY_VERSION = "1"


@dataclass(frozen=True)
class FeatureDefinition:
    name: str
    source: str
    source_schema_version: int


MARKET_FEATURE_NAMES = (
    "close_usd",
    "volatility_6h_pct",
    "volatility_24h_pct",
    "volatility_7d_pct",
    "range_24h_avg_pct",
    "volume_zscore_7d",
    "rsi_14",
    "momentum_6h_pct",
    "momentum_24h_pct",
    "momentum_7d_pct",
    "hour_utc",
    "weekday_utc",
    "hour_sin",
    "hour_cos",
    "weekday_sin",
    "weekday_cos",
)
DERIVATIVES_FEATURE_NAMES = (
    "derivatives_funding_rate_pct",
    "derivatives_open_interest_usd",
    "derivatives_oi_change_1h_pct",
    "derivatives_oi_change_24h_pct",
    "derivatives_long_liquidation_usd_1h",
    "derivatives_short_liquidation_usd_1h",
    "derivatives_liquidation_total_usd_1h",
    "derivatives_liquidation_imbalance",
)
MICROSTRUCTURE_FEATURE_NAMES = (
    "microstructure_spread_bps",
    "microstructure_bid_depth_usd_10bps",
    "microstructure_ask_depth_usd_10bps",
    "microstructure_imbalance_10bps",
    "microstructure_bid_depth_usd_25bps",
    "microstructure_ask_depth_usd_25bps",
    "microstructure_imbalance_25bps",
    "microstructure_microprice_deviation_bps",
)
CROSS_ASSET_FEATURE_NAMES = (
    "cross_eth_return_1h_pct",
    "cross_eth_return_6h_pct",
    "cross_eth_return_24h_pct",
    "cross_eth_btc_relative_6h_pct",
    "cross_eth_btc_relative_24h_pct",
    "cross_btc_eth_corr_24h",
    "cross_btc_eth_corr_168h",
    "macro_vix_level",
    "macro_vix_change_5d_pct",
    "macro_us10y_yield_pct",
    "macro_us10y_change_5d_bp",
)


def _definitions() -> dict[str, FeatureDefinition]:
    groups = (
        (MARKET_FEATURE_NAMES, "market_data", 1),
        (DERIVATIVES_FEATURE_NAMES, "derivatives_signals", 1),
        (MICROSTRUCTURE_FEATURE_NAMES, "microstructure_signals", 1),
        (CROSS_ASSET_FEATURE_NAMES, "cross_asset_signals", 1),
    )
    return {
        name: FeatureDefinition(name, source, schema_version)
        for names, source, schema_version in groups
        for name in names
    }


FEATURE_REGISTRY = _definitions()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _lineage(features: Iterable[FeatureDefinition]) -> str:
    payload = {
        "registry_version": FEATURE_REGISTRY_VERSION,
        "features": [
            {
                "name": feature.name,
                "source": feature.source,
                "source_schema_version": feature.source_schema_version,
            }
            for feature in sorted(features, key=lambda item: item.name)
        ],
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def resolve_feature_set(feature_names: Iterable[str]) -> dict[str, Any]:
    names = sorted(set(feature_names))
    unknown = sorted(set(names) - set(FEATURE_REGISTRY))
    if unknown:
        raise ValueError(f"enabled features are not registered: {', '.join(unknown)}")
    features = [FEATURE_REGISTRY[name] for name in names]
    return {
        "registry_version": FEATURE_REGISTRY_VERSION,
        "version": f"feature-set-v{FEATURE_REGISTRY_VERSION}-{_lineage(features)[:16]}",
        "lineage_sha256": _lineage(features),
        "enabled_features": names,
        "sources": {
            source: schema_version
            for source, schema_version in sorted(
                {(feature.source, feature.source_schema_version) for feature in features}
            )
        },
    }


def validate_feature_set(feature_set: Mapping[str, Any]) -> None:
    enabled = feature_set.get("enabled_features")
    if not isinstance(enabled, list) or not all(isinstance(name, str) for name in enabled):
        raise ValueError("feature set must contain enabled feature names")
    if len(enabled) != len(set(enabled)):
        raise ValueError("feature set contains duplicate enabled features")
    resolved = resolve_feature_set(enabled)
    for field in ("registry_version", "version", "lineage_sha256", "sources"):
        if feature_set.get(field) != resolved[field]:
            raise ValueError(f"feature set has incompatible {field}")
