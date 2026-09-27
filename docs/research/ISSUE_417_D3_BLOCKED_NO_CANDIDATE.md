# Issue #417: D3 blocked — no qualifying candidate

**Status: blocked. D3 was not launched.** Issue #416 closed its candidate family with zero candidates and no winner, so the explicit #417 no-candidate condition applies. No candidate/champion pair was frozen and no D3 origins, forecast attempts, or scored pairs exist.

The machine-readable [JSON report](ISSUE_417_D3_BLOCKED_NO_CANDIDATE.json) is the accounting record. Its source digests are verified by the focused test. This is a blocked outcome, not a D3 accuracy evaluation.

## Recorded outcome

- Candidate: null; champion: null; candidate/champion pair: null.
- D3 launch: no; status: blocked; origins: 0; exact pairs at 2h/4h/8h/16h: 0.
- Every metric is null because no D3 scoring took place.
- No accuracy peeks or D3 outcome access occurred. No production model, forecast, or configuration changed.
- Source evidence: #411 reports zero confirmatory forecasts and zero matured pairs; #416 records zero actual candidate attempts/scores and zero candidates/winner. The #410 protocol remains the frozen scoring reference.

## Reopening contract

D3 may be reconsidered only after all of the following are frozen, hashed, and established before the first D3 score is examined:

1. **One selected pair and complete preregistration:** #416 must select exactly one candidate that qualifies under its frozen selection contract. Freeze candidate and champion identities, candidate configuration, model/checkpoint, policy, code revision, data/source and vintage lineage, start/end and fixed evaluation cutoff, stopping rule, practical-effect threshold, and all D3 metrics/criteria. Hash the complete preregistration before any D3 score access.
2. **Independent exact-pair cohort:** D3 origins and outcomes must be disjoint from candidate development, inner and outer folds, and the final holdout. For candidate, champion, persistence, and strongest naive, use identical exact UTC `(origin_at, target_at, horizon)` pairs, shared source vintage/targets, and the same scoring rules.
3. **Minimum duration and maturity:** collect at least 30 calendar days and at least 200 exact matured pairs separately at each of 2h, 4h, 8h, and 16h. Report dependence and effective sample size; leave the gate blocked if any requirement is unmet.
4. **Complete accounting and frozen inference:** retain every forecast, failure, retry, and exact maturity status. Apply the frozen family-adjusted uncertainty procedure and criteria for primary effect, baseline skill, per-horizon regression, directional loss, and interval safety. Preserve failures and do not silently discard attempts.
5. **No repeated accuracy peeking:** access only predeclared operational/health checks before the fixed cutoff and final analysis. Do not repeatedly test accuracy or stop early for favorable results. Report every interruption or source-vintage change and handle it under the frozen plan.
6. **Human-reviewed disposition only:** report blocked, inconclusive, negative, or eligible for human review with artifacts and hashes. Eligibility is not approval; no automatic promotion, public output change, or configuration mutation is permitted.

These conditions restate the #417 acceptance contract. A future evaluation requires new qualifying #416 evidence and a separately frozen D3 plan; this report authorizes no scoring.
