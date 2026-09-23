# Roadmap v5 context/configuration experiment status

**Decision: blocked/inconclusive; retain the existing 168/336/512-hour production
configuration. No candidate was selected and no production behavior changes.**

## Candidate inventory

The experimental `timesfm_multi_context_inventory` helper can replay the
predeclared return-context candidates (64, 168, 336, 512 and 1024 steps), records
both return-step and underlying candle durations, and skips unavailable windows
instead of dropping origins from a comparison. Z-normalization restores its
training-context mean and scale to all predicted return quantiles before price
reconstruction. Symmetric averaging is applied only to explicitly marked
inner-selected contexts; candidate selection itself must be supplied by an
inner chronological evaluation, not inferred from context size.

The current codebase does not contain the frozen D1 BTC/USD candle corpus
(2023-01-01 through 2026-08-31, with 180-day warm-up), its hash manifest, or
outer-fold predictions needed to run this experiment. The available short
production ledger is D2 diagnostic data with mixed configuration cohorts, not
an untouched OOS test. No post-freeze D3 observations exist yet. The synthetic
unit tests use a TimesFM stub and verify input/output mechanics only; they are
not model-quality evidence.

## Required experiment before a retain/drop decision

1. Restore and hash the frozen, validated same-venue BTC/USD D1 data and list
   missing/revised candles. If unavailable, retain this blocked status; do not
   substitute BTCUSDT without labeling it as transfer evidence.
2. Use the same eligible origins for every context, include warm-up without
   scoring it, and use nested chronological folds with at least a 16-hour
   purge. Freeze target semantics before selection.
3. Compare each context to the existing univariate champion and persistence.
   On inner folds only, select no more than two contexts for staged
   normalization/symmetric-averaging tests. Report per-horizon loss, bias,
   interval score, residual dependence, inference time and memory.
4. Test one-shot versus matched-horizon inference and batch settings only when
   they are not implementation-equivalent; require declared numerical
   equivalence tolerances. Preserve all failed/missing forecast origins.
5. Freeze any winner before prospective D3 confirmation; no earlier data may
   be described as confirmation.

Until these data and runs are available, the inventory is a testable mechanism,
not evidence that a longer context, normalization, symmetric averaging or any
combination improves forecasting.
