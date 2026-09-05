# Conformal interval calibration

Production interval calibration uses normalized nonconformity scores from matured ensemble forecasts. For each 2h/4h/8h/16h horizon, the score is `abs(actual - point) / historical_half_width`. The finite-sample conformal quantile is selected for the configured target coverage and applied to the current base interval width.

## Safety and fallback

Only matured outcomes are eligible. Production reads durable outcomes attached to historical snapshots; walk-forward evaluation can supply only target candles that were available at each simulated origin. Current-regime observations are preferred when at least `BTC_CONFORMAL_MIN_SAMPLES` are available; otherwise calibration falls back across regimes. If the total sample is still sparse, the previous empirical coverage multiplier remains in use.

Defaults:

- `BTC_INTERVAL_TARGET_COVERAGE=0.80`
- `BTC_CONFORMAL_HISTORY_LIMIT=200`
- `BTC_CONFORMAL_MIN_SAMPLES=20`
- conformal multiplier safety bounds: 0.50–3.00

## Evaluation

`forecast.json` includes an `interval_calibration_evaluation` section for each horizon. It reports sample count, source, target coverage, empirical coverage before/after calibration, average historical interval width before/after conformal scaling, and the corresponding legacy multiplier/width. This makes the production change directly comparable with the previous heuristic method.

The calibration path never consumes an unmatured future outcome. In walk-forward evaluation, callers must pass only the history and target candles known at the forecast origin.
