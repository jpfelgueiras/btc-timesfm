# Diversified non-TimesFM model

`ridge_features` is a lightweight direct multi-horizon ridge-regression model intended to add a materially different inductive bias to the TimesFM-heavy ensemble.

For each 2h/4h/8h/16h target it trains on past-only engineered features: lagged log returns, 6/24/72h momentum, mean returns and volatility, hourly range, volume z-score, RSI, and cyclical hour/week signals. A separate ridge fit predicts cumulative log return for each horizon. Training examples are admitted only when the target close already exists in the supplied market window, so the implementation is safe for chronological walk-forward evaluation.

The model uses only NumPy and runs in milliseconds relative to TimesFM inference. Return extrapolation is bounded by recent realized volatility to fail conservatively under ill-conditioned feature windows.

## Production gate

The model is **research-only by default**. Backtest/optimizer entrypoints include it so standalone performance, residual correlation, and ensemble contribution can be measured on the existing purged walk-forward path. Production only includes it when:

```text
BTC_ENABLE_DIVERSIFIED_MODEL=true
```

That flag should only be set after the existing statistical-evidence / promotion-policy reports show defensible out-of-sample value. Adding the code therefore does not silently change the production ensemble.

Other configuration:

- `BTC_RIDGE_MIN_TRAIN_SAMPLES=96`
- `BTC_RIDGE_ALPHA=8.0`

Once enabled, `ridge_features` is returned through the same baseline/model-prediction interface as the statistical baselines, so adaptive and correlation-aware weighting automatically evaluate it under the same floors, caps, persistence fallback and residual-correlation diagnostics.
