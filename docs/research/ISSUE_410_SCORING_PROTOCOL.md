# Issue #410 — frozen v11 prospective scoring protocol

**Protocol:** `v11-prospective-1.0.0` · **Status:** frozen before evaluation
**Machine-readable contract:** [JSON](ISSUE_410_SCORING_PROTOCOL.json) · [JSON Schema](ISSUE_410_SCORING_PROTOCOL.schema.json)

The JSON artifact is authoritative. Its `protocol_sha256` is SHA-256 over
canonical UTF-8 JSON of the complete object excluding that digest field (sorted
keys, compact separators, no non-finite numbers). Verify it with
`btc_timesfm.research.frozen_scoring_protocol.load_frozen_protocol`. This
protocol was designed without inspecting any v11 model score. Current canonical
BTC/USD corpus/replay prerequisites remain blocked; this artifact authorizes no
score inspection, promotion, or claim of skill.

## Estimands and attempt accounting

The primary point estimand is the equally weighted mean across the four
predeclared horizons of each horizon's mean absolute cumulative log-return
error: `r=ln(P_actual/P_origin)`, `r_hat=ln(P_hat/P_origin)`,
`abs(r_hat-r)`. It is scale-free, additive in return space, and cannot be
switched for a more favorable price metric. Secondary point measures are
absolute price-percent error, signed log-return bias, signed price-percent
bias, and directional error. Direction is undefined when either realized or
predicted return has absolute magnitude at most `1e-6`; such rows remain in
point-loss denominators and are counted separately for direction.

Interval estimation is separate. Report q10/q50/q90 pinball losses, the
80%-nominal Winkler score, empirical coverage and absolute and origin-price
normalized width. Coverage alone is not probabilistic skill; proper scores and
sharpness are required alongside calibration. No interval result substitutes
for the primary point test.

Every attempt is retained as scored, failed, abstained, missing, or ineligible.
Invalid/nonpositive prices are failed, never imputed. Paired skill requires
exactly matching UTC `(origin_at,target_at,horizon_hours)`, vintage and outcome
identity; score only shared successful pairs, and separately report all
attempt counts/rates. Any candidate failure-rate regression blocks eligibility.
No nearest-time join or naive timestamp is permitted.

## Comparators, design, and inference

Required comparators are the production champion frozen at preregistration,
persistence, exact-hour seasonal-naive 24h, and the strongest simple baseline
selected only using inner training data from drift_7d, drift_24h, AR(1), and
EMA-return-24h. Inner choices are frozen before outer outcomes mature; ties
break lexicographically. Regime and training-only volatility-tercile strata and
cutpoints are declared before evaluation.

Use nested expanding walk-forward selection, a 16-hour maturity purge plus
24-hour embargo, a separately reserved untouched chronological holdout, and a
distinct prospective D3 shadow (at least 30 days and 200 mature exact pairs per
horizon). Register the immutable vintage, candidates, complete attempt family,
cutoff and access controls before evaluation. One analysis occurs after all
planned outcomes mature. No optional stopping, interim score access, or
post-hoc threshold, strata, candidate, or multiplicity changes are allowed.

The practical floor is at least 3% relative primary-loss reduction versus each
required comparator. Simultaneous per-horizon primary-loss regression may not
exceed 5% of comparator mean loss; directional error-rate increase may not
exceed 2 percentage points. Uncertainty is paired moving-block bootstrap over
24-origin blocks (covering maximum 16-hour target overlap), 10,000 replicates,
fixed seed 4102026, with identical blocks for all comparisons. Report raw pairs
and effective independent blocks. Holm family-wise adjustment at 0.05 covers
all registered candidates, horizons, required comparators, confirmatory point
and interval tests, and predeclared strata. Fewer than 200 paired origins or
eight effective blocks in any confirmatory horizon/comparison means
blocked/inconclusive, not permission to pool away the shortfall.

Permitted outcomes are blocked, inconclusive, negative, and eligible for human
review. Eligibility is not approval; there is no automatic promotion. Any
future contract change requires a new version/hash before an independent
evaluation; previous observations remain exploratory.

## Verification

Run `PYTHONPATH=src python -m unittest tests.research.test_frozen_scoring_protocol`.
The checks validate the committed hash, exact pairing and duplicate rejection,
failure/missing handling, UTC and origin cutoff enforcement. They do not load
forecast scores or execute an evaluation.
