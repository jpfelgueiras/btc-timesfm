# Forecast benchmark suite

The backtest pipeline evaluates the production forecast on the same forecast origins, horizons, regimes and scoring rules as a deterministic benchmark suite. The goal is to make simple alternatives the default reference point before additional model complexity is rewarded.

## Included baselines

- `persistence` — random walk with zero drift; every horizon equals the latest observed close. This is the primary baseline and is always present.
- `drift_7d` — the existing capped seven-day mean log-return drift baseline.
- `drift_24h` — a shorter capped 24-hour mean log-return drift baseline.
- `seasonal_naive_24h` — repeats the observed price from the matching hour of the previous day. For supported horizons up to 16 hours, the referenced value is always known at forecast time.
- `ar1` — the existing capped first-order autoregressive return baseline.
- `ema_return_24h` — projects a 24-hour-span exponentially weighted mean log return, capped using recent volatility.

## Evaluation

Every baseline is scored with the same metrics used for production models:

- mean absolute percentage error (MAE %)
- mean signed error / bias (%)
- direction accuracy
- sample count

Results are reported independently for 2h, 4h, 8h and 16h horizons and are also segmented by the market regime detected at each forecast origin. The backtest report records the best benchmark by MAE, the adaptive ensemble's MAE delta versus persistence, and its delta versus the best benchmark.

The benchmark configuration is included in the experiment manifest so a historical report can be reproduced with the same reference models.

## Leakage safety

All benchmark inputs are restricted to the market context available at the simulated forecast origin. In particular, `seasonal_naive_24h` uses `t + h - 24` for horizon `h`; because all supported horizons are below 24 hours, it never reads a future candle.

## Canonical BTC/USD corpus gate (issue #339)

The existing replay downloader uses Binance BTCUSDT, which is a transfer cohort and
must not be described as a BTC/USD result. The canonical benchmark bootstrap audits
an explicitly supplied, single-venue hourly BTC/USD CSV and never downloads or
silently substitutes another market. Required columns are `timestamp` (ISO-8601
with UTC offset, denoting candle close), `venue`, `pair`, `open`, `high`, `low`,
`close`, `volume`, `vintage`, and `revision`. `vintage` is the UTC time that exact
candle became available and must be no later than its close; `revision` must be
exactly `0`. Any later vintage, nonzero revision, or duplicate candle timestamp
blocks the dataset; the audit never selects a revision using later knowledge. The
configured target is `[TARGET_START, TARGET_END)` (2023-01-01 through 2026-08-31
UTC); every hourly target observation and every hour of the immediately preceding
180-day warm-up is required. The report states actual, expected, and missing
target/warm-up hours. The audit also checks hourly alignment, OHLCV validity, gaps,
venue/pair consistency, and SHA-256. Passing this gate is not
forecast evidence: production-parity replay, exact origin pairing, failures, and
dependence-aware uncertainty remain required. Research ridge remains disabled.

```bash
PYTHONPATH=src python -m btc_timesfm.research.canonical_benchmark \
  --data /path/to/immutable_btc_usd_hourly.csv \
  --output canonical_benchmark_audit.json
```

Without `--data`, the command emits a machine-readable `blocked` result with zero
observations. The current checkout contains no canonical corpus; its limited recent
Kraken/Bitstamp feeds and the BTCUSDT replay downloader do not establish the
required multi-year same-venue BTC/USD history. Do not interpret the current
historical BTCUSDT replay or the audit gate as evidence of forecasting skill.

Run the normal walk-forward backtest to produce `backtest_report.json`:

```bash
PYTHONPATH=src python -m btc_timesfm.research.backtest --days 90 --samples 60
```

The report exposes benchmark results under `summary.<horizon>.benchmarks` and regime-segmented results under `summary.<horizon>.benchmarks_by_regime`.
