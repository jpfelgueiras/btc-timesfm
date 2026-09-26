# Production market-data policy

Production forecasts use two independent hourly BTC market-data sources:

- **Primary:** Kraken BTC/USD hourly OHLC.
- **Fallback:** Bitstamp BTC/USD hourly OHLC.

Both providers are normalized to UTC candle-close timestamps and the common `MarketData` OHLCV shape. Market-data validation is applied independently to each provider before selection.

## Selection and failover

When Kraken is healthy, it remains the selected source. If Bitstamp also returns data, the most recent overlapping closes are compared. Any disagreement beyond the configured tolerance fails the run closed rather than choosing either provider, even if Kraken passed validation.

When Kraken is unavailable at the network/provider level and returns no data, a healthy Bitstamp dataset may be used directly because there are no primary candles to compare. If Kraken returns data but fails validation (for example because it is stale), Bitstamp fallback is allowed only when there is sufficient overlap and the comparison is within tolerance. If overlap is insufficient, failover is rejected. A detected disagreement already aborts selection; it is not a condition under which fallback is permitted.

If neither source is healthy, or if the providers disagree beyond tolerance, the production forecast stops before TimesFM is loaded and nothing is posted to X.

## Configuration

The default cross-provider policy is intentionally conservative:

| Variable | Default | Purpose |
| --- | ---: | --- |
| `BTC_PROVIDER_MAX_CLOSE_DIFF_PCT` | `0.75` | Maximum allowed close-price difference on any compared candle. |
| `BTC_PROVIDER_COMPARE_CANDLES` | `24` | Maximum number of recent overlapping candles to compare. |
| `BTC_PROVIDER_MIN_OVERLAP` | `6` | Minimum overlap required when failing over from an unhealthy-but-readable primary. |

Validation thresholds remain controlled by the existing `BTC_DATA_*` variables.

## Provenance and diagnostics

Every successful `forecast.json` records `source`, `source_pair`, and `market_data_provenance`. The durable history database already persists the forecast `source`, so historical forecasts identify whether Kraken or the fallback supplied the input.

The production run emits `market_data_source.json`, containing provider health, validation results, fallback status, and cross-provider comparison metrics. The selected provider's validation report is written to `market_data_validation.json`.
