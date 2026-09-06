# Crypto derivatives signals

Issue #34 adds timestamp-safe BTC derivatives context without making the production forecast depend on an external derivatives provider.

## Providers

- **Funding rate:** Binance USD-M `BTCUSDT` perpetual funding history.
- **Open interest and liquidations:** Gate USDT `BTC_USDT` perpetual contract statistics.

Both APIs are public and require no repository secret. Provider failures are isolated: the spot forecast continues with a `partial` or `unavailable` derivatives status.

## Timestamp and freshness rules

Every snapshot is bounded to the latest completed spot-candle timestamp. Rows after that forecast origin are discarded before any feature is derived. Funding older than 12 hours and contract statistics older than 2.5 hours are treated as stale and omitted.

The normalized feature set is:

- `derivatives_funding_rate_pct`
- `derivatives_open_interest_usd`
- `derivatives_oi_change_1h_pct`
- `derivatives_oi_change_24h_pct`
- `derivatives_long_liquidation_usd_1h`
- `derivatives_short_liquidation_usd_1h`
- `derivatives_liquidation_total_usd_1h`
- `derivatives_liquidation_imbalance`

The funding/open-interest/liquidation values are normalized raw measurements. OI changes, liquidation total, and imbalance are derived values. Available values are merged into `market_features` and therefore persist with the immutable forecast origin in durable SQLite history. `forecast.json` also retains provider provenance, freshness, missing-feature diagnostics, and the latest bounded provider rows.

Production does **not** change ensemble weights or predictions merely because derivatives data is available. This avoids promoting a signal before out-of-sample evidence exists.

## Walk-forward ablation

`derivatives_ablation.py` compares the same ridge forecaster with two feature sets:

1. current spot-market features only;
2. the exact same features plus all derivatives features.

For every simulated origin, a training row is eligible only when its target timestamp is already observable for the horizon being evaluated. The report includes MAE, bias, direction accuracy, paired statistical evidence, and a conservative `edge_detected` / `no_defensible_edge` recommendation for 2h, 4h, 8h, and 16h.

Run locally with:

```bash
python derivatives_ablation.py --days 30 --samples 96 --min-train 48
```

The scheduled/manual GitHub Actions workflow publishes `derivatives_ablation_report.json` and `derivatives_ablation_summary.md`. This research result is evidence for later feature-selection work; it does not automatically alter production.
