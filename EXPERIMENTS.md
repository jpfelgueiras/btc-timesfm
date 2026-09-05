# Experiment reproducibility

Every production forecast and walk-forward backtest records a versioned `experiment_manifest` so a historical result can be tied back to the code, model, configuration and exact input window that produced it.

## Manifest contents

The manifest records:

- `manifest_version` and a unique `run_id`
- a deterministic `configuration_id` derived from the canonical forecast/backtest configuration
- a deterministic `data_id` derived from the exact OHLCV bytes used by the run
- Git commit SHA and working-tree dirty state when available
- TimesFM model ID and installed package version
- forecast horizons, TimesFM context windows and adaptive-weighting parameters
- run-specific parameters such as requested backtest days/sample count
- source/provider identity, source pair, candle count and first/last candle timestamps
- Python, NumPy and Torch seed values
- Python/platform runtime information

`run_id` is intentionally unique per execution. `configuration_id` and `data_id` are the stable comparison keys: two runs using the same configuration and exact market-data window will expose matching IDs even when their execution timestamps differ.

## Production forecasts

`forecast.json` contains the complete manifest at:

```text
experiment_manifest
```

The same manifest is copied into the rolling scheduler snapshot and persisted in the durable SQLite history. Schema version 3 adds these fields to `forecast_origins`:

```text
experiment_run_id
configuration_id
experiment_manifest_json
```

Because forecast origins are first-write-wins, a manual rerun for an already-recorded origin cannot replace the manifest associated with the original historical prediction.

The normal CSV/JSONL history exports include the run/configuration IDs and full JSON manifest.

## Backtests

`backtest_report.json` contains the manifest at:

```text
experiment_manifest
```

A backtest manifest records the requested history length and walk-forward sample count, the exact Binance BTCUSDT history window/digest, model configuration and code revision.

## Reproducing a historical production forecast

1. Read `experiment_manifest.code.git_sha` from the stored forecast/history row.
2. Check out that commit.
3. Install the project dependencies from that revision.
4. Restore/retrieve the market data identified by `experiment_manifest.data.source`, `pair`, first/last candle timestamps and `ohlcv_sha256`.
5. Re-run the forecast with the configuration and seed values recorded by the manifest.
6. Verify the reproduced market-data digest and `configuration_id` match the historical manifest before comparing predictions.

The public provider may revise historical candles, so matching `ohlcv_sha256` is the strongest check that the exact same input was recovered. If the digest differs, the run is not byte-for-byte reproducible even when the time window is the same.

## Reproducing a backtest

For a report created with the standard CLI, use the recorded Git SHA and the values under `configuration.run_parameters`:

```bash
git checkout <experiment_manifest.code.git_sha>
pip install -r requirements.txt
python backtest.py --days <days_requested> --samples <samples_requested>
```

Then compare the new report's `configuration_id` and `data_id` with the historical report. A matching configuration ID with a different data ID means the provider returned different historical OHLCV values or a different input window.

## Deterministic seeds

Production forecasting and backtesting call `seed_everything()` before model loading. Python, NumPy and Torch (when installed) receive the recorded seed. This removes avoidable pseudo-random variation; external provider revisions and library/model implementation changes are still captured separately by the data digest, dependency versions and Git SHA.
