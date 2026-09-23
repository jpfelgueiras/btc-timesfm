# Roadmap v5 persistence-first ensemble experiment status

**Decision: blocked/inconclusive; retain frozen production policy and do not
promote a shrinkage or member-removal candidate.**

## Diagnostic evidence (D2 only)

Issue #257's paired short-ledger audit reports ensemble vs persistence MAPE of
0.324108% vs 0.324401% (2h), 0.496325% vs 0.496569% (4h), 0.682988% vs
0.676938% (8h), and 0.946709% vs 0.910505% (16h), with paired sample sizes
92–95. The audit explicitly identifies this as descriptive mixed-version
diagnostic evidence, not corrected/significant/deployable skill. The
persisted direction flags are contaminated by rounded near-zero moves, so no
directional reward was optimized here. These data motivate the persistence
control; they do not establish an ensemble win or a drop decision.

The frozen D1 same-venue BTC/USD corpus, exact origin-level forecast variants,
and nested outer-fold losses are not included in this checkout. D3 prospective
confirmation is also not mature. Thus no candidate can yet satisfy the
predeclared nested OOS/persistence/strongest-baseline and adjusted-CI gates.

## Research-only candidate mechanics

`research.persistence_shrinkage` defines a bounded catalog of at most seven
policy shapes: frozen production weights supplied by the caller; 100%
persistence; equal model weights; equal family weights (TimesFM contexts
treated as one family); and three convex family-to-persistence shrinkage
ratios. Its research-only normalizer allows exact zero weights, permits a
100%-persistence fallback, and fails on invalid/infeasible caps instead of
silently restoring production floors. It is not wired into production and does
not fit or select weights.

## Required evaluation before selection

1. Freeze the validated D1 dataset, venue/vintage/hash and supported-origin
   set; preserve identical origins and all failed forecasts.
2. Correct signed/directional scoring and use return-scale errors, not rounded
   USD deltas. Compare <=8 predeclared policies on exact paired horizons.
3. Fit any shrinkage ratio only on inner OOF residuals in nested chronological
   folds with >=16h purge. Score untouched outer folds against frozen
   production, persistence and the strongest inner-selected simple baseline.
4. Ablate TimesFM contexts, drift, AR and each adaptive scoring term separately;
   quantify incremental paired value rather than treating low correlation as
   benefit. Keep point and interval scores separate.
5. Verify sparse-history/failure cases and exact zero/full-persistence paths.
   Apply V5 gates and reserve frozen D3 for prospective confirmation.

No model is removed or added to production on the basis of the small D2 sample.
