# Correlation-aware ensemble weighting

The production ensemble first computes the existing durable adaptive weights, then applies a conservative diversification overlay using rolling **signed residual correlations** for the same forecast horizon.

Only paired matured forecasts are eligible. Residuals are aligned by forecast origin, so no future target can enter a correlation estimate. Positive residual correlations contribute to a redundancy score; negative correlations are not penalized. Each penalty is shrunk toward 1 while samples are sparse, then the existing 3% floor / 55% cap normalization is reapplied.

Defaults:

- `BTC_CORRELATION_HISTORY_LIMIT=120`
- `BTC_CORRELATION_MIN_SAMPLES=12`
- `BTC_CORRELATION_FULL_SAMPLES=36`
- `BTC_CORRELATION_PENALTY_STRENGTH=0.55`
- `BTC_CORRELATION_MAX_BLEND=0.70`

When paired history is insufficient, weighting is exactly the current adaptive policy. Drift/static fallback behavior remains authoritative.

## Evaluation

Weighting diagnostics include the base adaptive weights, pair sample counts, residual correlation matrix, redundancy score, penalty and final weight for every model. Walk-forward evaluation reports the current adaptive ensemble and the correlation-aware ensemble on the same validation origins, alongside persistence and the rest of the benchmark suite.
