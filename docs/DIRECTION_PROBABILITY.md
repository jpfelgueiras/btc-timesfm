# Calibrated probability-of-direction forecasts

Issue #113 replaces bare UP/DOWN/NEUTRAL categorical labels with calibrated directional probabilities (`P(up)`, `P(down)`, `P(|move| > threshold)`) for each 2h/4h/8h/16h horizon. The probability is a real calibrated chance of the outcome, unlike `forecast_confidence`, which is an evidence-quality band.

## Calibration method

For each model+horizon the estimate is the smoothed historical frequency of up/down/meaningful-move outcomes, conditioned on the cohort of past matured forecasts whose predicted direction matches the current forecast's predicted direction. A beta-posterior-style smoothing shrinks toward the neutral 0.5 prior, so sparse cohorts fall back rather than emitting overconfident probabilities.

## No-look-ahead guarantee

Only matured durable-history rows whose `target_at` is already <= the forecast origin are eligible. No outcome realized after the forecast-time cutoff can influence the estimate. The `leakage_guard` block in `forecast.json` records the input/output columns used.

## Diagnostics

- **Brier score** — mean squared error of calibrated probabilities against realized up/down outcomes, computed with leave-one-out probabilities so a sample does not vote on its own calibration;
- **reliability table** — predicted-probability bins (`[0.0,0.1)`, ... `[0.9,1.0]`) with count, mean predicted frequency and observed frequency;
- **calibration error (ECE)** and max bin deviation.

## Evidence and public claims

A horizon is `calibrated` (and `reliable`) only with at least 20 matured cohort samples; otherwise it is `sparse_fallback` and `reliable=false`. The overall `public_claim_allowed` flag is `false` whenever any horizon lacks sufficient evidence, so no calibrated-probability claim is made about horizons that are not yet supported by matured history.

## Output

`forecast.json` exposes the section under `direction_probability`:

```text
direction_probability.version
direction_probability.public_claim_allowed
direction_probability.horizons.<horizon>.{p_up,p_down,p_move}
direction_probability.horizons.<horizon>.calibration_state
direction_probability.horizons.<horizon>.reliable
direction_probability.horizons.<horizon>.brier_score
direction_probability.horizons.<horizon>.reliability
```

Implementation: `src/btc_timesfm/forecasting/direction_probability.py`, tests in `tests/forecasting/test_direction_probability.py`.