# Roadmap v5 regime/recency experiment status

**Decision: blocked/inconclusive; retain the current policy and do not promote
regime/recency changes.**

The research-only `elapsed_recency` helper provides time-based exponential
weights for matured outcomes, rejects any outcome beyond the decision cutoff,
and computes Kish effective sample size. It addresses one mechanical issue with
row-count decay under irregular schedules; it does not define a production
weight policy, select half-lives, or establish a forecast-quality gain.

No immutable D1 same-venue USD dataset and nested outer-fold forecasts are
present in this checkout. The short D2 production ledger is diagnostic and
cannot choose among conditioning policies. Its changing code/configuration
cohorts and sparse regime observations are not an untouched evaluation. D3
prospective confirmation has not reached the required frozen duration. Thus no
reportable evidence supports removing or retaining particular regime or
recency terms based on skill.

## Bounded evaluation required

Predeclare no more than four policies: no regime conditioning; the current
three-state regime; one elapsed-time recency rule with an inner-selected
half-life; and that same recency rule with a validated persistence-first
fallback. Fit thresholds, pooling and hysteresis on inner folds only. Replay
irregular schedules and missing-origin outages, purge at least 16 hours plus
label delay, and ensure future-outcome mutation cannot change an earlier
decision. Report horizon and origin-time trend/volatility/transition segments,
worst-segment loss, effective sample size, update churn and recovery lag with
dependence-aware paired uncertainty. Future shocks may label diagnostic
episodes only; they cannot influence decisions at the origin.

Keep low-powered segments explicitly inconclusive and freeze any winning policy
before D3. This branch changes no production weights, detector, or fallback.
