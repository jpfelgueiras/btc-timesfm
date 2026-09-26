# TimesFM context, horizon, and checkpoint comparison (#344)

**Status: blocked.** The harness does not infer or make a forecast-skill claim.
Run `PYTHONPATH=src python -m btc_timesfm.research.context_comparison` to write
`context_comparison_report.json`. It performs no network requests or checkpoint
downloads and does not modify the production configuration.

The bounded context catalog is 64, 168, 336, 512, and 1024 hourly log returns.
Each window uses one additional candle for its physical lookback (for example,
336 returns require 337 closes). The production checkpoint remains
`google/timesfm-3.0-pytorch` revision
`43046b85ec22d584a13f8098c2ed39c889e129c2`, CPU, batch size 1, 16-step
prediction, quantiles enabled, and symmetric averaging disabled. Only the
168/336/512 contexts are production contexts. A newer checkpoint remains
unauthorized until explicitly configured with an immutable revision and
verified artifact SHA-256; public availability alone is not eligibility.

## Staged experiment

1. Compare eligible context lengths under the pinned checkpoint, with matched
   physical lookbacks and exactly identical forecast origins.
2. Use nested chronological folds (outer test untouched) to select at most two
   contexts on inner folds. Then test horizon-specific context assignment.
   Normalization and symmetric averaging are separate later factors.
3. Test a newer checkpoint only after its API contract supports this inference
   path and its authorization, immutable revision, and artifact hash are
   verified. Compare one-call versus horizon-specific calls only if returned
   outputs differ.
4. Compare against the unchanged production context ensemble and persistence.
   Acceptance requires at least 3% out-of-sample improvement with the required
   dependence-aware confidence interval, and all existing per-horizon safety
   gates. Failed and missing origins must remain visible in the report.

The canonical frozen corpus from #339 is currently blocked/absent. Prior V5
context comparisons (#265) therefore remain blocked, and #343 also has no
eligible corpus or independently supported head. The default report marks the
corpus and exact artifact SHA identity unavailable, does not pretend variants
are eligible, and does not fabricate predictions or comparisons. Supply a
reviewed corpus manifest, exact artifact identity, and matched inference
records before any statistical comparison; do not use the optimizer's unrelated
scores as context evidence.
