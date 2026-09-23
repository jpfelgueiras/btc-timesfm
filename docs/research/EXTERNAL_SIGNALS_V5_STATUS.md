# Roadmap v5 ETH/derivatives experiment status

**Decision: blocked/inconclusive; external inputs remain passive metadata and
are not enabled as forecast covariates or residual corrections.**

The existing code collects ETH, funding, open-interest and liquidation data,
but the frozen D1 BTC/USD candle corpus and versioned, timestamped historical
external-source vintages required for paired as-of evaluation are not present
in this checkout. The D2 production ledger is diagnostic and does not provide
an untouched external-feature holdout. The inspected forecast path appends
optional signals after point inference, so current production forecasts are
not evidence that those signals helped. This branch does not claim feature
coverage, response, or incremental forecast skill.

## Source inclusion gates

No source group is accepted without an audited row-level timestamp, publication
latency, availability, units, missingness, revision behavior, outage rate and
cost scorecard. The initially bounded order is ETH/USD 1h and 6h relative
returns, then funding plus OI change only if their as-of coverage is sufficient.
Gate OI/liquidation fields must retain verified units; unit fallbacks must not
be silently labeled USD and missing liquidation fields must not be encoded as
true zeros. Books require prospective capture and are not historically
reconstructed. FRED/macro and further sources remain deferred.

## Required experiment before a decision

1. Build a versioned availability ledger and prove each feature was available
   at the exact forecast decision cutoff; preserve common-available and
   all-origin cohorts separately.
2. Freeze an interface for causal covariates or regularized residual correction
   and training-only scaling/missingness. Keep market-only, deployed champion
   and persistence controls fixed.
3. Use nested leave-group-out chronological evaluation, same symbol/venue,
   exact origin/target pairing, lag/outage stress tests, failed-origin
   accounting and dependence-aware CIs. Report every horizon/segment and runtime
   cost. D1 may only use truly reconstructable as-of history; otherwise collect
   D3 prospectively and wait for the registered minimum.
4. An unavailable group must reproduce market-only output exactly. No source is
   activated without passing the V5 point-loss gates and the separate frozen
   D3 confirmation.

The full D1 feature experiment is therefore not scoreable from available
artifacts; do not describe standalone-ridge ablations as incremental value
over the deployed champion.
