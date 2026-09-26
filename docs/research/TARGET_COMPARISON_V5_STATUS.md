# Forecast target and return-path comparison status (#343)

**Decision: blocked.** No target winner, forecast-quality result, or production
change is supported by the current corpus and model contract.

The bounded report utility is
`btc_timesfm.research.target_comparison_experiment`. It reconstructs a price
path from hourly log returns by causal cumulative summation and retains
transformed incumbent outputs only as round-trip diagnostics. Those transforms
are not independently fitted candidates. The report therefore marks direct
cumulative-return, normalized-price, and log-price heads unavailable unless a
manifest explicitly supplies both an eligible corpus and a supported direct
head; the codebase currently supplies neither. It does not fabricate direct
forecasts or claim evidence from transformed incumbent values.

The canonical benchmark audit (`research.canonical_benchmark`) currently finds
no eligible immutable, same-venue BTC/USD corpus (the gate requires complete
2023-01-01 through 2026-08-31 target coverage plus 180 days of warm-up,
hour-aligned vintage/revision provenance, and no gaps). The audit without a
data file reports zero observations and `blocked`. TimesFM's production
`TimesFM3Evaluator.predict_batch` contract accepts return contexts and returns
the incumbent path; there is no supported separately supervised target-head
training/inference API. Thus no honest candidate comparison can presently be
run.

When prerequisites exist, the comparison must freeze the production hourly
return path and persistence baseline; align candidate origins, 168/336/512
return context availability, inputs, and inference capacity; use nested purged
walk-forward validation with exact matured target timestamps; preserve failed
origins; and report per-horizon loss, direction, interval semantics, and
dependence-aware paired evidence. Each target needs an independently fitted
and forecast head. Accumulating hourly q10/q50/q90 marginal paths independently
is a diagnostic only: those marginals do not determine the joint cumulative
return distribution, so resulting bands are not automatically calibrated
cumulative quantiles. One-step reconstruction uses only returns through that
step and the origin price.

Acceptance remains >=3% out-of-sample improvement with dependence-aware
evidence, positive performance versus persistence and the strongest naive
baseline, no >5% regression at another horizon, and no >2 percentage-point
direction regression. Until the corpus and supported independent head exist,
the report decision is `blocked`; the V5 conclusion remains inconclusive.
