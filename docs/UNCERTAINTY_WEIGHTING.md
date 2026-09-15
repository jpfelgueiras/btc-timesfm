# Uncertainty-aware ensemble weighting

Issue #131 evaluates whether a calibrated-uncertainty overlay improves the
production ensemble. The candidate combines three sources of evidence into a
single bounded weight vector:

1. **Out-of-sample error history** — the existing adaptive weighting scores.
2. **Residual correlation** — the correlation-aware diversification overlay.
3. **Calibrated interval/probability uncertainty** — a penalty for models whose
   matured Q10-Q90 intervals are miscalibrated, especially overconfident models
   that are narrow but under-covering, plus a directional Brier component.

The current adaptive and correlation-aware ensembles remain the mandatory
baselines; this module never changes production defaults. Evaluation is
**report-only** and routed through the existing paired-bootstrap significance
policy.

## Weight formula

`uncertainty_aware_model_weights` first computes the correlation-aware weights
(the adaptive error-history scores plus the residual-correlation overlay), then
scales each model by a sample-shrunk calibration penalty:

```
penalty = 1 - blend * (1 - exp(-UNCERTAINTY_PENALTY_STRENGTH * calibration_error))

calibration_error = mean(interval_coverage_error, DIRECTION_CALIBRATION_WEIGHT * direction_brier_error)

blend = MAX_CALIBRATION_BLEND * progress(samples)
progress = clamp((samples - MIN_CALIBRATION_SAMPLES) / (CALIBRATION_FULL_SAMPLES - MIN_CALIBRATION_SAMPLES), 0, 1)
```

The interval component is the absolute error of empirical Q10-Q90 coverage vs
`TARGET_INTERVAL_COVERAGE` (0.80); the probability component is the
leave-one-out Brier error of the smoothed historical directional probability.
Both are computed only from matured outcomes whose target predates the forecast
origin (no lookahead).

A dominance guard then caps any leader that holds more than
`MAX_TOTAL_DOMINANCE_RATIO` over the runner-up unless it has both enough matured
samples and non-overconfident calibration. The existing 3% floor / 55% cap
normalization is reapplied last, so every safeguard in production remains
authoritative.

Defaults:

- `BTC_UNCERTAINTY_HISTORY_LIMIT=200`
- `BTC_UNCERTAINTY_TARGET_COVERAGE=0.80`
- `BTC_UNCERTAINTY_MIN_SAMPLES=6`
- `BTC_UNCERTAINTY_FULL_SAMPLES=24`
- `BTC_UNCERTAINTY_PENALTY_STRENGTH=0.60`
- `BTC_UNCERTAINTY_MAX_BLEND=0.70`
- `BTC_UNCERTAINTY_OVERCONFIDENCE_GAP=0.15`
- `BTC_UNCERTAINTY_DIRECTION_WEIGHT=0.50`
- `BTC_UNCERTAINTY_MAX_DOMINANCE_RATIO=2.0`
- `BTC_UNCERTAINTY_MIN_EVIDENCE_SAMPLES=32`

The full formula, its hyperparameters and every safeguard are versioned in the
JSON-serializable `weighting_formula()` block embedded in every evaluation
report and every registered experiment manifest, so results are reproducible.

## Fallback and safeguards

- **Sparse calibration history** (fewer than `MIN_CALIBRATION_SAMPLES` matured
  intervals) yields a `sparse_fallback` calibration state and a penalty of `1.0`,
  leaving the correlation-aware base unchanged.
- **Insufficient overall history / static-prior base** keeps the base policy
  unchanged (`uncertainty_mode: base_policy`).
- **Dominance guard** (`dominance_guard`) prevents a single overconfident or
  thinly-evidenced model from holding an out-sized lead without calibrated
  evidence. A lead larger than `MAX_TOTAL_DOMINANCE_RATIO` over the runner-up
  is clipped unless the leader has `MIN_DOMINANCE_EVIDENCE_SAMPLES` matured
  samples and is not flagged overconfident.

## Evaluation

`evaluate_uncertainty_weighting` replays frozen out-of-sample forecasts through
the adaptive, correlation-aware, uncertainty-aware and persistence policies on
identical origins, reporting MAE, direction accuracy, interval coverage and
coverage error by **horizon** and by **regime**, chronological-bucket stability,
and paired-bootstrap significance (`uncertainty_vs_correlation_aware`,
`uncertainty_vs_adaptive`, `uncertainty_vs_persistence`). Only matured outcomes
are visible to any weight computation at each origin.

CLI:

```bash
PYTHONPATH=src python -m btc_timesfm.research.uncertainty_weighting \
  --samples-json samples.json --actuals-json actuals.json \
  --output uncertainty_weighting_report.json \
  --markdown uncertainty_weighting_report.md \
  --registry-db experiments.db
```

Reports register into the experiment registry as `candidate` runs via
`register_from_report` with a versioned reproducibility manifest and audit
SHA-256.