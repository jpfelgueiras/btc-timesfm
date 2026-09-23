# Roadmap v5 coherence/direction-flip ablation status

**Decision: blocked/inconclusive; production coherence and publication gates
remain unchanged.**

The research helper `preserve_valid_marginals` demonstrates the candidate
contract: each horizon must have finite, ordered q10/q50/q90 values, but valid
marginals may have rising q10 or falling q90 across horizons. Tests cover both
non-nested acceptance and within-horizon crossing rejection. The helper is not
used by the production forecast path.

The available D2 ledger retains published outputs after reconciliation, not the
raw pre-reconciliation intervals and all confidence/direction stage values
needed to replay the candidate fairly. The frozen D1 same-venue BTC/USD data
and per-origin outer scores are not in this checkout. D3 cannot yet meet its
prospective minimum. Consequently, no D1 paired interval-score, turning-point,
availability, or forecast-utility result can be reported; no constraint has
been removed from publication.

## Required evaluation before changing policy

1. Persist pre/post-coherence q10/q50/q90, point/change/direction/confidence,
   guardrail failures and policy IDs at each origin.
2. On identical paired origins compare current reconciliation, within-horizon
   ordering only, and valid raw predictions. Score interval WIS/pinball,
   coverage/width, point loss, turning-point precision/recall/delay, call
   availability and failed publications.
3. Use a frozen nested chronological D1 test and dependence-aware paired CIs;
   do not choose on D2. Preserve valid reversals and ensure dependent direction
   and confidence fields refer to the final candidate output.
4. Retain the current policy unless the preregistered material WIS or
   availability criterion passes without protected point/calibration
   degradation; then freeze before D3 confirmation.

No non-nesting property is described as mathematically impossible or as evidence
of better calibration. The helper only establishes the independent-marginal
validation behavior for controlled research.
