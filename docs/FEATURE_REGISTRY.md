# Feature Registry and Data Lineage

The versioned feature registry provides a contract for feature definitions and data lineage, ensuring reproducibility for production forecasts and backtests.

## Feature Registry

The registry is defined in `src/btc_timesfm/forecasting/feature_registry.py`. It holds:
- Feature definitions (`name`, `source`, `source_schema_version`)
- Groupings for market, derivatives, microstructure, and cross-asset features.

## Data Lineage Contract

The registry provides a lineage contract for each feature set, allowing to:
- Resolve a set of feature names to a `feature_set` dictionary including a version and a SHA-256 lineage hash.
- Validate a `feature_set` dictionary against the current registry contract.

The lineage hash ensures that if any feature definition or the set of enabled features changes, the lineage hash changes, thus flagging the feature set as incompatible if not updated.
