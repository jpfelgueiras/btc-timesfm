# Stacked ensemble

`research/stacked_ensemble.py` evaluates a research-only meta-learner over the current
production correlation-aware ensemble, the horizon-specialized policy, and the
regime-specialist policy. It does not change production weights or promotion state.

## Leakage controls

The stack uses `build_purged_walk_forward_folds` over forecast origins. For every fold,
training labels mature before validation begins; `assert_no_fold_leakage` verifies this.
The non-negative simplex ridge meta-learner is fitted independently per horizon using
only that fold's training specialist predictions. Validation predictions are therefore
strictly out of fold.

## Report

`evaluate_stacked_ensemble(samples, actual_by_timestamp, ...)` returns reproducible fold
definitions and learned weights, plus paired-bootstrap MAE comparisons against production
and persistence. Results are segmented by horizon and detected regime. Segments below the
configured sample threshold, or whose confidence interval is inconclusive, are explicitly
marked `inconclusive`; they are not promotion evidence.

The input uses the existing frozen forecast sample schema from regime-specialist research.
The report includes a deterministic, registry-compatible `experiment_manifest` with a
configuration digest, metrics, and `promotion_mode: shadow_only`. It can be placed in the
experiment registry or attached to a champion/challenger shadow run without making a
production change.

## Forecast uncertainty surface

Production forecast records now publish `model_disagreement_usd` and
`model_disagreement_pct` alongside `model_agreement` for each horizon. These
machine-readable dispersion signals can be consumed by uncertainty-aware weighting and
the site without conflating a research stack evaluation with a deployed forecast policy.
