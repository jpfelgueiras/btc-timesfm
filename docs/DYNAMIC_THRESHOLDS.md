# Dynamic neutral and no-edge thresholds

Issue #115 replaces the single fixed 0.25% neutral/no-edge band with per-horizon
bands derived from the ensemble's own matured history. The thresholds quantify how
small a predicted BTC move must be before it is statistically indistinguishable
from noise at a given 2h/4h/8h/16h horizon.

## How thresholds are learned

For every horizon the module selects only durable-history rows whose
`target_at` already passed at the forecast origin. From those matured rows it
computes:

- **realized volatility** — sample standard deviation of `actual_change_pct`;
- **drift statistic** — one-sample mean test of realized moves against zero,
  giving a standard error, t-statistic, and p-value;
- **directional probability edge** — a binomial-style difference-of-counts test
  of `n_up` vs `n_down` against the 50/50 chance baseline, using the calibrated
  `P(up)` and `P(down)` from issue #113.

The neutral threshold is `z_95 * sigma / sqrt(n)` and the stricter no-edge
threshold is `z_99 * sigma / sqrt(n)`, both clamped between `min_neutral`
(default 0.05%) and `max_neutral` (default 2.0%).

When evidence is insufficient (< 20 samples or near-zero realized volatility)
the fixed 0.25% fallback is used instead, so behaviour degrades safely rather
than emitting an overconfident narrow band.

## No-look-ahead guarantee

A row is eligible only if:

1. It belongs to the correct `model_name` and `horizon_hours`.
2. Its `target_at` is already ≤ the forecast origin.
3. Its `predicted_change_pct` and `actual_change_pct` are finite.

No outcome observed after the forecast origin can influence the learned
thresholds.

## Suppression of directional claims

Each per-horizon block classifies the current prediction and decides whether to
suppress a public bullish/bearish claim:

- **inside the no-edge band** → suppress, because the predicted move is too
  small to distinguish from noise;
- **insufficient matured samples** → suppress, to avoid overconfident narrow
  thresholds;
- **calibrated probabilities indistinguishable from chance** → suppress, because
  historical direction frequency does not exceed the 50/50 baseline.

The top-level `public_directional_claim_allowed` flag is `false` whenever any
horizon is suppressed.

## Fixed-baseline evaluation

Every per-horizon block also runs `evaluate_against_fixed`, which reclassifies
all matured rows with both the learned threshold and the old fixed 0.25%
threshold, producing:

- agreement rate between the two classification schemes;
- count of reclassified directional→neutral and neutral→directional rows;
- direction accuracy under each scheme.

This allows retrospective comparison of the dynamic thresholds against the
status-quo fixed band.

## Minimum-sample and evidence safeguards

| Safeguard | Default | Purpose |
|-----------|---------|---------|
| `min_evidence_samples` | 20 | Minimum matured rows before thresholds are learned |
| `min_neutral_threshold_pct` | 0.05% | Floor on the learned neutral band |
| `max_neutral_threshold_pct` | 2.0% | Ceiling on the learned neutral band |
| `max_no_edge_threshold_pct` | 4.0% | Ceiling on the stricter no-edge band |
| `significance_alpha` | 0.05 | p-value threshold for drift and proportion tests |

## Output

`forecast.json` exposes the section under `dynamic_thresholds`:

```text
dynamic_thresholds.version
dynamic_thresholds.public_directional_claim_allowed
dynamic_thresholds.suppressed_horizons
dynamic_thresholds.horizons.<horizon>.neutral_threshold_pct
dynamic_thresholds.horizons.<horizon>.no_edge_threshold_pct
dynamic_thresholds.horizons.<horizon>.threshold_source
dynamic_thresholds.horizons.<horizon>.edge_status
dynamic_thresholds.horizons.<horizon>.suppress_directional_claim
dynamic_thresholds.horizons.<horizon>.suppress_reasons
dynamic_thresholds.horizons.<horizon>.baseline_evaluation
dynamic_thresholds.horizons.<horizon>.statistical_evidence
dynamic_thresholds.horizons.<horizon>.calibrated_evidence
dynamic_thresholds.overall
dynamic_thresholds.leakage_guard
```

Implementation: `src/btc_timesfm/forecasting/dynamic_thresholds.py`, tests in
`tests/forecasting/test_dynamic_thresholds.py`.
