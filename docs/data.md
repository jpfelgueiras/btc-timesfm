# Data

This page summarizes the production data path and distinguishes model inputs from
context recorded alongside a forecast and from research-only feature proposals.
Detailed provider and feature-family guides are linked below.

## Production spot data

The point forecasting model receives **one input series: hourly BTC/USD log
returns**. Returns are differences of log closing prices; TimesFM predicts
returns, which are accumulated from the latest close to produce price forecasts.
It does not receive OHLCV columns or the feature-registry groups as model
covariates.

Kraken BTC/USD (`XBTUSD` at the API) is the primary spot source. Bitstamp BTC/USD
is the fallback; Binance BTCUSDT is not the production spot fallback. Both
providers are fetched and validated, and where both return data their overlapping
closes are checked for disagreement. A healthy Kraken series is selected by
default. A healthy Bitstamp series can be used on a primary outage, or after a
readable but unhealthy primary only if sufficient overlap agrees. Excessive
disagreement, or the absence of a healthy source, stops forecast generation.
Selection and validation diagnostics record the selected provider, fallback
status, validation results and comparison (`market_data_source.json` and
`market_data_validation.json`). See [market-data policy](MARKET_DATA.md) for
provider thresholds and provenance.

Hourly data is represented by **candle-close timestamps in Unix seconds**,
interpreted as UTC. Bitstamp candle-open timestamps are advanced by one hour to
normalize them to close time. The current/incomplete candle is excluded. The
latest completed candle timestamp defines the forecast origin and is the time
against which other observations are bounded.

OHLCV validation checks aligned array lengths, finite and positive prices and
volume, valid high/low bounds, strictly increasing unique timestamps, hourly
cadence, freshness (90-minute default maximum age), future timestamps (60-second
tolerance), and extreme price moves (20% hourly return and 30% candle range by
default). Duplicate, out-of-order, missing/irregular, stale, future, malformed,
or extreme-price data fails validation; it is not silently repaired or
interpolated for production inference. The separate gap-reconciliation tooling
can assess provider-covered gaps and provenance-marked backfills, but that is
distinct from the forecast input path. Volume outliers alone are a soft
exception: when extreme volume is the only validation error, historical-median
winsorization caps those values before feature engineering and the validation
report records the warning and count. Prices and timestamps are never winsorized.

## Feature groups and when they are used

| Group | Examples | Production role |
| --- | --- | --- |
| Point-model series | Hourly BTC/USD log returns | The only input series supplied to TimesFM. |
| Spot regime/report features | Close, candle range, volume z-score, realized volatility, RSI, momentum, and UTC hour/weekday calendar encodings | Derived from validated spot OHLCV for forecast orchestration (including regime classification), reporting, attribution, and calibration; not TimesFM covariates. |
| Optional passive metadata | ETH/cross-asset and macro context; derivatives funding, open interest and liquidations; order-book/microstructure measurements | When available and healthy, appended to `market_features` after forecast inference and retained with the forecast/manifest. Availability alone does not change the production point prediction or model weights. |
| Research-only probes | Lower-timeframe 5m/15m multi-resolution aggregates and feature-family/interaction/selection probes | Evaluated in bounded research or walk-forward experiments; they are not active production point-model inputs. |

The [feature registry](FEATURE_REGISTRY.md) defines names and lineage for market,
derivatives, microstructure and cross-asset metadata. It is a recording and
reproducibility contract, not a declaration that every registered feature is
fed to the point model. See the detailed [derivatives](DERIVATIVES_SIGNALS.md),
[cross-asset](CROSS_ASSET_SIGNALS.md), [microstructure](MICROSTRUCTURE_SIGNALS.md),
and [multi-resolution research](MULTI_RESOLUTION.md) guides.

## Optional-source health and history limits

Derivatives, cross-asset/macro and microstructure sources are fallible. Health
checks compare capture age with the forecast origin (default optional-source
maximum age 2.5 hours), check missing-feature completeness, and detect changed
same-origin snapshots. Stale, incomplete or revised sources are quarantined;
provider disagreement can also quarantine market data. Quarantined feature
groups are excluded while a valid spot forecast can continue. The CLI records
source status, freshness, fallback, quarantine reasons and exclusions in
`source_health.json` and the forecast output.

Optional snapshots are retained under `.state/optional_source_retention.json`
for a bounded default of 168 hours and 336 records. This supports limited
first-observed replay, not a full historical archive. Most external providers
do not expose point-in-time vintages or historical order books, so old external
values cannot generally be reconstructed as they were known at each past
forecast origin. Retention is prospective and short-lived; it does not provide
robust long-run point-in-time backtesting or establish incremental predictive
value. Feature promotion requires separate, leakage-safe research evidence.

The production entry point selects and validates spot data, fetches optional
snapshots, evaluates health and retention, then runs forecast inference:

```bash
PYTHONPATH=src python -m btc_timesfm.cli.btc_forecast
```

For full CLI and operational details, see [getting started](getting-started.md)
and [operations](operations.md).
