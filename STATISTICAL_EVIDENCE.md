# Statistical evidence for model comparisons

The weekly optimizer uses paired bootstrap confidence intervals before it recommends a candidate configuration for review.

## Pairing

Every comparison is paired by forecast origin. Candidate, production, and persistence errors must refer to the same chronological origins. The optimizer rejects a comparison if candidate and production origin lists differ.

For each origin the optimizer records:

- mean MAE across the 2h, 4h, 8h, and 16h horizons;
- direction accuracy across those horizons;
- MAE for each individual horizon;
- persistence MAE across the same horizons.

This prevents an apparent improvement caused only by comparing different market periods.

## Bootstrap method

`statistical_significance.paired_bootstrap_comparison()` resamples paired origins with replacement using a deterministic NumPy RNG seed. The default policy is:

- 5,000 bootstrap iterations;
- 95% confidence interval;
- at least 32 paired origins before drawing a directional conclusion.

The reported improvement is always oriented so that positive values favor the candidate. For MAE this is `baseline - candidate`; for direction accuracy it is `candidate - baseline`.

Each result reports the candidate and baseline means, raw metric delta, mean improvement, relative effect size, paired standardized effect when defined, confidence interval, bootstrap probability that the candidate is better, sample count, conclusion, and reason.

## Conclusions

A result is `candidate_better` only when the minimum paired sample count is met and the full confidence interval for improvement is above zero. It is `baseline_better` when the full interval is below zero. Otherwise it is `inconclusive`. Runs below the minimum sample count are always explicitly marked `inconclusive` with reason `insufficient_samples`.

## Promotion evidence

The optimizer remains recommendation-only. Its existing performance guardrails still apply, and two statistical checks are now added:

1. the candidate MAE improvement over production must be statistically supported;
2. there must not be statistical evidence that persistence is better than the candidate.

The optimizer report schema is version 2 and stores the complete significance result under `comparison.significance`, including overall MAE, direction accuracy, per-horizon MAE, and candidate-vs-persistence evidence. The guardrail section records the bootstrap settings used for the decision.
