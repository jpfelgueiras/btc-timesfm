# Roadmap v5 native TimesFM covariates: experiment status

**Decision: blocked; do not enable covariates or claim incremental value.**

This repository's production forecast currently calls TimesFM with the return
context and no exogenous covariates. The offline environment has not produced a
verified run against the pinned model artifact showing the supported covariate
shapes, forecast parity, or actual covariate response. A test file that imports
the large model, generates random data, prints whether results differ, and
returns `True` is not a deterministic regression test or evidence; it was
removed rather than treated as verification.

The repository does not contain the frozen D1 BTC/USD dataset (2023-01-01 to
2026-08-31 plus warm-up), its data-vintage manifest, or outer-fold forecasts.
The available short live ledger is D2 diagnostic only. It has no historical
native covariate arrays and cannot establish a matched OOS comparison. D3
prospective evidence cannot yet meet the required 30-day window. Consequently,
there is no supportable `keep` or `reject` decision for log-volume changes,
realized volatility/range, UTC calendar features, or their combination.

## Work required to unblock

1. Verify the covariate API and exact package/model artifact from pinned source
   or a deterministic integration run. Record package, artifact revision,
   supported shapes and model license/usage eligibility.
2. Add pure causal feature builders aligned one-to-one with hourly log returns:
   log-volume changes; rolling realized volatility/OHLC range; and UTC hour/day
   calendar features. Only calendar values may extend into the forecast horizon.
3. Fit scaling and missingness policy on each training fold only. Include
   future-mutation tests and exact univariate/no-feature baseline parity.
4. Run matched nested chronological OOS comparisons for each group and at most
   one inner-selected combination against the same champion, univariate model,
   ridge control, and persistence. Add leave-group-out, lagged and shuffled
   controls; preserve failed origins and report runtime/availability.
5. Report the full dataset lineage, per-origin predictions, fold results,
   paired adjusted uncertainty, and a keep/reject/inconclusive decision. Freeze
   any candidate before the required prospective D3 period.

No feature is added to the production forecast by this issue branch. A changed
forecast from an arbitrary covariate is not evidence of improved forecasting.
