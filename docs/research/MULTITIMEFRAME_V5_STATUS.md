# Roadmap v5 native multi-timeframe experiment status

**Decision: blocked/inconclusive; retain the hourly production forecast and
make no 15m/4h model or blend recommendation.**

The checkout contains OHLCV multi-resolution summary/ablation utilities, but no
immutable finest-resolution BTC/USD corpus with retained provider/vintage
lineage and aligned 15m/1h/4h TimesFM predictions. The required D1 hourly USD
corpus and outer predictions are not present; the short D2 live ledger has no
matched lower-timeframe model outputs and is not an untouched holdout. D3
latency evidence cannot satisfy its prospective minimum yet. No native
multi-timeframe candidate was run or scored here.

The required 1h/15m comparison must use identical wall-clock origins and target
closes, UTC-aligned completed-bar aggregation, and explicitly record the
resolution-specific lookback duration, patch length and horizon units. A
4-hour model may only be scored at compatible exact endpoints (4h/8h/16h);
2-hour targets must not be interpolated and relabeled as native forecasts.
Existing lower-timeframe summary features remain the cheaper control. Standalone
skill and residual complementarity must be established before any fixed convex
blend; blend weights are inner-OOF only.

## Unblock requirements

1. Freeze and hash the same-venue BTC/USD fine-resolution dataset, publication
   schedule, revisions, gaps and aligned eligible-origin set. If coverage is
   absent, report actual supported dates and keep the experiment blocked rather
   than substituting BTCUSDT or inventing rows.
2. Add deterministic aggregation tests for partial bars, gaps, day boundaries,
   completed-candle cutoffs and exact UTC close timestamps.
3. Compare hourly, native 15m, optional compatible 4h, and existing 5m/15m
   summary controls on identical origins with the frozen target and stronger
   baselines. Use nested purged chronological folds; fit blends only from inner
   OOF residuals.
4. Persist failures, availability, per-origin outputs, data/model manifests,
   dependence-aware paired metrics and inference latency/cost. Freeze before
   prospective D3; do not claim current summary-feature work is a native-model
   comparison.

No inference or production behavior is changed by this blocked status report.
