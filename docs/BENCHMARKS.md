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

Run the normal walk-forward backtest to produce `backtest_report.json`:

```bash
python backtest.py --days 90 --samples 60
```

The report exposes benchmark results under `summary.<horizon>.benchmarks` and regime-segmented results under `summary.<horizon>.benchmarks_by_regime`.
