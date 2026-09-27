# Issue #412 — TimesFM context/target evaluation blocked

## Outcome: blocked; no scoring or hypothesis

The prerequisite in #412 is an adequately powered, measured context/horizon
failure from #411. The frozen #411 readiness report passes protocol validation,
but it does not identify such a failure: its historical corpus and prospective
evidence gates are blocked, all reported metrics are null, and it has zero
confirmatory forecasts, zero expected pairs, and zero matured pairs. Its last
verified prospective snapshot had 48 timestamp-eligible forecasts, all 48
excluded as legacy/unversioned; the newest provenance-complete origin at
2026-09-27 11:00 UTC was not mature. Therefore no TimesFM context or target
policy was scored, and this record does not invent a candidate hypothesis.

No candidates or folds were preregistered or run. All metrics and the winner
are null; no holdout was accessed, and no production code, configuration, or
forecast policy was changed. This is a blocked research disposition, not a
negative result about any context or target policy.

Machine-readable disposition: [JSON](ISSUE_412_TIMESFM_CONTEXT_BLOCKED.json).

## Evidence snapshot and hashes

The source is the checked-in #411 report at
[`ISSUE_411_PARITY_BASELINES_BLOCKED.json`](ISSUE_411_PARITY_BASELINES_BLOCKED.json),
SHA-256 `c1b3471495c0c32bcb8a5a90ae7fe5a4e20a6bdcb5d4d617e8d83e8be18202a7`.
It records the #410 protocol gate as passed, while corpus and eligible mature
production-policy evidence remain blocked. The #397 canonical corpus audit
records 0 of 32,136 target hours and 0 of 4,320 warm-up hours; its SHA-256 is
`e2bf0c671a5a4327c6852d02614a7d2e403ed12f8fe7c4362336c5e87d44a0c7`.

The frozen protocol is
[`ISSUE_410_SCORING_PROTOCOL.json`](ISSUE_410_SCORING_PROTOCOL.json), version
`v11-prospective-1.0.1`; file SHA-256
`1b8c6705d53882a66c0ab1277cf2a63c845d00c2c7d0cc978c0c75d8121efd18`, protocol
digest `4da936d505df9a15cbd073ee85ee0e4ed2db483e6058f6b81ce163f4724bb4f0`.
The report's last-verified prospective snapshot supplies code SHA-256
`79a95fc3debd4134ab68cfc55795007a340f15a6eff2ef7212a875a9846c28fc`, database
SHA-256 `b102f14db535264ecc2474a3b338c929abea4c39616f233a5701611c9300f57f`, and
cohort SHA-256
`462287eb433a27c16e4f25360d9ee40e5dda2fabdced13e5390859a055f7bbb8`. These are
the hashes and counts as reported by the frozen #411 artifact, not a new
evaluation performed for #412.

## Exact reopening gates and next action

1. Resolve the #411 corpus and eligible mature production-policy evidence
   blockers, then rerun its frozen readiness protocol and retain the report and
   source hashes.
2. Require that eligible, mature, exact-key #411 diagnosis name a concrete
   context/horizon failure and meet #410's minimum of 200 paired origins per
   horizon and 8 effective independent blocks per comparison. A missing or
   underpowered diagnosis remains blocked/inconclusive; do not infer one.
3. Before viewing any #412 candidate scores, preregister the diagnosis-linked
   bounded candidate family, model/checkpoint identity, outcomes/comparisons,
   fixed folds, all exclusions and failures, stopping rule, practical effect,
   and complete multiplicity family.
4. Evaluate on nested earlier purged walk-forward folds with exact shared UTC
   `(origin_at, target_at, horizon)` and input-vintage keys. Lock a chronological
   final holdout disjoint from candidate selection and all prior evaluations;
   preserve it untouched until the preregistered final analysis.

**Next action:** do not score or propose context/target candidates now. First
resolve #411 and obtain its adequately powered measured diagnosis. Only then
preregister the bounded #412 design and disjoint holdout before any scoring.
Any later research recommendation remains separate from production changes.
