# Roadmap v5 issued direction probability status

**Decision: blocked/inconclusive; no probability-calibration or selective-use
promotion claim.**

The current `direction_probability` code computes current probabilities from
matured rows available by the supplied cutoff, but its historical Brier and
reliability diagnostics are leave-one-out recalculations from a later-known
history. Those diagnostics are retrospective, not the probabilities actually
issued at each old forecast origin. The module documentation now labels that
distinction explicitly.

Historical forecast rows do not persist the full issued P(up/down/neutral),
neutral/move thresholds, confidence inputs, abstention decision and immutable
probability-policy identity required to score an issuance as sent. Legacy
probability history therefore cannot be reconstructed after the fact without
lookahead. D1 issuance/revelation replay data are not available in this
checkout; D2 cannot be treated as untouched and lacks full original issued
probability records. D3 has not completed its prospective minimum. No Brier,
reliability, selective-coverage, or calibration improvement result is asserted.

## Required implementation/evaluation before a decision

1. Persist exact issued P(up/down/neutral), point forecast, thresholds,
   confidence inputs, abstention/call decision, code/configuration/data IDs,
   and policy version with each immutable `(origin, horizon)` prediction.
2. At maturity, score those exact stored probabilities; mark legacy missing
   values unavailable. Retain leave-one-out values as clearly labeled
   retrospective diagnostics only.
3. Compare the current cohort shrinkage, rolling base rate, and one bounded
   regularized calibrator. Fit calibrator/thresholds on inner OOF observations
   only; evaluate fixed 25/50/75/100% call coverage, all-origin denominators,
   risk-coverage curves and meaningful-move cost controls.
4. Test future-history/future-label mutation, missing/stale inputs, sparse
   cohorts, immutable persisted values, deterministic replay and cold starts.
   Require matched-coverage selective error and adjusted uncertainty before any
   recommendation; freeze the policy before D3.

Point forecast accuracy, probability calibration, selective error and economic
utility remain separate claims. This report changes no issued probability or
abstention behavior.
