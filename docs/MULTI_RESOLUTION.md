# Lower-timeframe market context

Issue #121 tests whether compact 5m/15m market summaries improve 2h/4h forecasts while hourly production forecasting keeps running as-is.

## Design

Hourly candles average away short-term moves. Instead of feeding raw high-frequency history into a model, `src/btc_timesfm/data/multi_resolution.py` derives a small, bounded set of aggregates from aligned 5m candles:

- **Short-term momentum:** last 1h and 4h returns.
- **Realized volatility:** std of 5m returns over 1h/4h/24h and std of 15m (downsampled) returns over 4h/24h.
- **Range:** average `(high - low) / close` over 1h and 24h.
- **Volume pressure:** 1h/24h volume z-scores and a normalized 1h log-volume slope.
- **Microstructure summary:** average close position within the high-low range `(close-low)/(high-low)` over 1h/24h and its 24h standard deviation as an order-flow imbalance proxy.

### Leakage safety

Every derivation is a pure function of candles whose close timestamp is at or before the forecast origin. Candles strictly after the origin are excluded before anything is computed, and features are aligned only to completed timestamps. `summarize_if_available` returns an availability flag when lower-timeframe data is absent, malformed, or missing fields, so production can always proceed on hourly-only features.

### Versioning and reproducibility

`feature_set_version()` produces a sha256-based feature-set version (matching the `feature_selection_pipeline` convention) from the aggregation recipe plus the feature names. The same input candles and origin always produce the same features and version. The full recipe digest is reported as `recipe_sha256` for provenance.

## Research evaluation

`src/btc_timesfm/research/multiresolution_ablation.py` runs a leakage-safe walk-forward ablation comparing an hourly-only ridge baseline with the same model augmented by the lower-timeframe aggregate group. It mirrors the other feature-family ablations:

- training rows are eligible only when their target timestamp has already matured at the simulated origin;
- each origin is scored identically for both feature sets (paired comparison);
- results are reported by horizon with `baseline_mae_pp`, `candidate_mae_pp`, `relative_mae_improvement`, direction accuracy deltas and a deterministic paired-bootstrap significance result;
- `by_horizon`, `feature_names`, `walk_forward_samples` and `overall` follow the shared ablation-report schema so `feature_selection_pipeline.py` can consume the report.

The entrypoint accepts synthetic high-frequency candle arrays directly (used by the unit tests) and a CLI that reads a JSON document of candles:

```bash
PYTHONPATH=src python -m btc_timesfm.research.multiresolution_ablation \
  --candles multiresolution_candles.json --samples 96 --min-train 32
```

The JSON layout:

```json
{
  "hourly": {
    "timestamps": [1767225600, 1767229200],
    "opens": [60000.0, 60050.0],
    "highs": [60090.0, 60100.0],
    "lows": [59940.0, 59980.0],
    "closes": [60050.0, 60080.0],
    "volumes": [12.0, 14.0]
  },
  "high_frequency": {
    "timestamps": [],
    "opens": [],
    "highs": [],
    "lows": [],
    "closes": [],
    "volumes": []
  }
}
```

## Cost bounds

`multiresolution_ablation.py` makes zero network requests: evaluation is a pure function over the candles you supply, the ridge design matrix is bounded (7 baseline + 15 aggregates), walk-forward samples default to 96 and bootstrap iterations are capped. The report includes `runtime_estimate_seconds` and an `api_cost` block documenting that runtime/API cost stays bounded for GitHub Actions.