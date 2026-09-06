# Production drift detection

Issue #33 adds a deterministic, leakage-safe drift layer in front of the production ensemble.

## What is monitored

Two independent signal families are evaluated before a new forecast is created:

- **Forecast-error drift** per `model_name × horizon`. Only durable predictions with a matured exact target candle are eligible. The detector compares the latest 8 matured outcomes with the preceding 24 outcomes for that same model/horizon.
- **Market-feature drift** for `volatility_24h_pct`, `range_24h_avg_pct`, `volume_zscore_7d`, `rsi_14`, and `momentum_24h_pct`. The latest 12 observed completed-candle feature vectors are compared with the preceding 36 vectors. The current completed candle may be included because it is already observed data, not a future outcome.

Missing or unmatured forecast outcomes are ignored. No target price can influence a drift decision before that target candle actually exists.

## Distribution metrics

Each rolling comparison records:

- baseline and recent sample counts
- baseline/recent medians
- median change
- robust scale based on baseline median absolute deviation
- absolute robust median shift (`robust_shift_z`)
- exact two-sample empirical-CDF distance (`ks_distance`)

Forecast-error signals additionally compare signed-error distributions and the drop in direction accuracy.

The implementation uses NumPy only; there is no hidden external statistical service or non-deterministic threshold fitting.

## Default thresholds

A signal is a **warning** when any of the following is true:

- robust shift >= `2.0`
- KS distance >= `0.35`
- forecast direction accuracy drops by >= `0.15`

A signal is **severe** when any of the following is true:

- robust shift >= `3.5`
- KS distance >= `0.55`
- forecast direction accuracy drops by >= `0.25`

The production state is the maximum severity across all evaluable signals. Windows and thresholds are written into every `drift_report.json`, so a decision can be reproduced later.

Environment overrides are supported through `BTC_DRIFT_ERROR_RECENT`, `BTC_DRIFT_ERROR_BASELINE`, `BTC_DRIFT_FEATURE_RECENT`, `BTC_DRIFT_FEATURE_BASELINE`, `BTC_DRIFT_WARNING_SHIFT_Z`, `BTC_DRIFT_SEVERE_SHIFT_Z`, `BTC_DRIFT_WARNING_KS`, `BTC_DRIFT_SEVERE_KS`, `BTC_DRIFT_WARNING_DIRECTION_DROP`, and `BTC_DRIFT_SEVERE_DIRECTION_DROP`.

## Production behavior

The drift state controls how strongly recent historical performance is allowed to change ensemble weights:

| State | Adaptive confidence | Behavior |
| --- | ---: | --- |
| none | 1.00 | normal adaptive weighting |
| warning | 0.50 | learned-weight blend is halved |
| severe | 0.00 | adaptive weighting falls back to the static regime prior |

The confidence values can be overridden with `BTC_DRIFT_WARNING_ADAPTIVE_CONFIDENCE` and `BTC_DRIFT_SEVERE_ADAPTIVE_CONFIDENCE`.

A severe state does **not** stop TimesFM inference or publishing by itself. It deliberately removes trust from recent adaptive performance estimates and uses the existing bounded static prior. Issue #39 can later consume the same severe drift state for stronger circuit-breaker behavior.

## Persistence and observability

Warning/severe drift events are stored in the durable forecast-history SQLite database. Events include the evaluation origin, experiment run identifier, signal key, severity, signal type, model/horizon or feature identity, and the full metric payload. Duplicate evaluation events for the same observed origin/signal/severity are ignored.

Every production run also writes:

- `drift_report.json` — machine-readable complete evaluation
- `drift_summary.md` — concise GitHub Actions summary
- `forecast.json.drift_detection` — the drift state used for that forecast
- structured observability events/counters for warnings and severe drift

Both drift files are included in the normal forecast Actions artifact.

## Testing

Synthetic tests cover stable distributions, large matured error shifts, current completed-candle feature shifts, deterministic threshold behavior, adaptive-confidence mapping, persistence, and schema migration. The synthetic error test also includes an unmatured future prediction to verify that it cannot affect the result.
