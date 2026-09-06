# Production market-data policy

Production forecasts use two independent hourly BTC market-data sources:

- **Primary:** Kraken BTC/USD hourly OHLC.
- **Fallback:** Binance BTCUSDT hourly klines. BTCUSDT is treated as a liquid USD proxy only when Kraken cannot safely supply the forecast input.

Both providers are normalized to UTC candle-close timestamps and the common `MarketData` OHLCV shape. Issue #17 validation is applied independently to each provider before selection.

## Selection and failover

When Kraken is healthy, it remains the selected source. If Binance is also healthy, the most recent overlapping closes are compared. A disagreement beyond the configured tolerance fails the run closed rather than choosing either provider.

When Kraken is unavailable at the network/provider level, a healthy Binance dataset may be used directly because no primary candles exist to compare. When Kraken returns data but fails validation (for example because it is stale), fallback is allowed only when enough overlapping candles exist and the cross-provider comparison is within tolerance.

If neither source is healthy, or if the providers disagree beyond tolerance, the production forecast stops before TimesFM is loaded and nothing is posted to X.

## Configuration

The default cross-provider policy is intentionally conservative:

| Variable | Default | Purpose |
| --- | ---: | --- |
| `BTC_PROVIDER_MAX_CLOSE_DIFF_PCT` | `0.75` | Maximum allowed close-price difference on any compared candle. |
| `BTC_PROVIDER_COMPARE_CANDLES` | `24` | Maximum number of recent overlapping candles to compare. |
| `BTC_PROVIDER_MIN_OVERLAP` | `6` | Minimum overlap required when failing over from an unhealthy-but-readable primary. |

Issue #17 validation thresholds remain controlled by the existing `BTC_DATA_*` variables.

## Provenance and diagnostics

Every successful `forecast.json` records `source`, `source_pair`, and `market_data_provenance`. The durable history database already persists the forecast `source`, so historical forecasts identify whether Kraken or the fallback supplied the input.

GitHub Actions also emits `market_data_source.json`, containing provider health, validation results, fallback status, and cross-provider comparison metrics. The selected provider's issue #17 validation report continues to be written to `market_data_validation.json`.
