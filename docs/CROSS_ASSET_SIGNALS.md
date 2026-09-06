# Cross-asset and macro market signals

Issue #36 adds a small, reproducible risk-context feature set without making the production forecast depend on external assets.

## Signals

Crypto-beta context comes from completed Kraken `ETHUSD` hourly candles aligned to the exact BTC forecast timestamps. The derived features are ETH 1h/6h/24h returns, ETH-vs-BTC relative strength at 6h/24h, and rolling BTC/ETH return correlations over 24h and 168h.

Macro risk context comes from public FRED series:

- `VIXCLS`: VIX close and five-observation percentage change;
- `DGS10`: US 10-year Treasury yield and five-observation change in basis points.

Daily macro observations are conservatively considered available only at **00:00 UTC on the following calendar day**. This intentionally sacrifices some freshness so backtests cannot accidentally use a same-day close before it was published. Values older than seven days are treated as stale.

## Production behavior

The production run fetches ETH and the two FRED series after the BTC market-data selection. Provider errors are isolated. Any available values are added to immutable `market_features`, while `forecast.json` and the experiment manifest retain provider, freshness, missing-feature and error diagnostics.

These signals do **not** alter production predictions or ensemble weights merely because they are available. A missing ETH or macro provider therefore cannot stop the forecast or X publication.

## Walk-forward ablation

`cross_asset_ablation.py` uses the recent bounded Kraken BTC/ETH history and FRED observations. At each simulated origin:

1. BTC feature engineering sees only candles available at that origin;
2. ETH rows newer than that origin are ignored;
3. FRED rows are filtered by the next-day availability rule;
4. a training example is eligible only after its target horizon has matured.

The same ridge forecaster is evaluated with the core spot features and with core + cross-asset/macro features on identical origins. Results are reported separately for 2h, 4h, 8h and 16h, include regime breakdowns, direction accuracy and deterministic paired-bootstrap uncertainty.

The scheduled research workflow retains the report for 90 days. A feature group is never promoted solely from in-sample fit; promotion is left to the feature-selection policy introduced by issue #37.
