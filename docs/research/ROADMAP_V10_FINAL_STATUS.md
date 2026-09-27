# Roadmap v10 — forecast evidence closeout

_Status dated: 2026-09-27_

Roadmap v10 completed its four implementation and audit workstreams (#397–#400).
The closeout is **blocked at empirical evaluation**: the required point-in-time
BTC/USD corpus is unavailable, the prospective evidence ledger has not
accumulated enough eligible mature forecasts, production-parity replay has no
metrics, and no candidate was preregistered or scored. This is not evidence for
or against an accuracy improvement. No winner was identified, and there was no
production configuration or forecast change.

## Workstream outcomes and dependencies

| Issue | Deliverable and evidence | Outcome / dependency |
|---|---|---|
| [#397](https://github.com/jpfelgueiras/btc-timesfm/issues/397) | [Corpus acquisition audit](ISSUE_397_PIT_BTCUSD_CORPUS.md) and [canonical machine-readable audit](ISSUE_397_CANONICAL_BENCHMARK_AUDIT.json). | **Blocked:** no admissible licensed, immutable, single-venue BTC/USD archive with point-in-time publication and revision provenance. Coverage is 0/32,136 target hours and 0/4,320 warm-up hours. Must pass before canonical replay. |
| [#398](https://github.com/jpfelgueiras/btc-timesfm/issues/398) | Prospective, append-safe shadow evidence ledger in `src/btc_timesfm/research/shadow_deployment.py`, schema v6. Forecast predictions and exact-target matured outcomes are write-once; reports account for eligible forecasts, maturity, failures, and versions. Runtime SQLite and status report belong under ignored `.state/` (default DB: `.state/shadow_deployment.sqlite`); no private runtime database is committed. | **Implemented; current ledger evidence unavailable:** no runtime database or status report was supplied to this closeout. The ledger is a prospective source of evidence, not an already populated empirical result. Its eligible, mature evidence is a prerequisite for #399. |
| [#399](https://github.com/jpfelgueiras/btc-timesfm/issues/399) | [Production-parity replay readiness contract](ISSUE_399_PRODUCTION_PARITY_REPLAY.md) and [blocked readiness report](ISSUE_399_PRODUCTION_PARITY_BLOCKED.json). | **Blocked:** #397 corpus gate fails and no #398 ledger report was supplied. The machine-readable report has null ledger hash/counts and `metrics: null`; no replay or canonical skill estimate is available. It depends on both #397 and #398 passing. |
| [#400](https://github.com/jpfelgueiras/btc-timesfm/issues/400) | [Candidate confirmation protocol](ISSUE_400_CANDIDATE_CONFIRMATION.md) and [blocked candidate report](ISSUE_400_CANDIDATE_CONFIRMATION_BLOCKED.json). | **Blocked:** parity diagnosis is unavailable; no candidates were preregistered or scored, and `candidate_winner` and `metrics` are `null`. Candidate selection depends on valid #399 evidence, followed by a separately frozen prospective confirmation. |

The dependency sequence is **#397 corpus + #398 accumulating prospective
ledger → #399 production-parity replay → #400 candidate preregistration,
scoring, and (only if selection criteria pass) disjoint prospective
confirmation**. The parallel #397 and #398 workstreams do not substitute for
one another: a historical admissible corpus is required for replay, while the
ledger accumulates contemporaneous, production-policy evidence.

## Remaining gates

1. **Corpus:** obtain and preserve the license, original files, acquisition
   manifest, and hashes for a single-venue BTC/USD hourly archive spanning the
   exact target interval `2023-01-01T00:00:00Z` through
   `2026-09-01T00:00:00Z` (32,136 hours) and contiguous warm-up interval
   `2022-07-05T00:00:00Z` through `2023-01-01T00:00:00Z` (4,320 hours). Every
   candle needs close time, first-publication time no later than close, and
   revision-0 provenance. Run the canonical audit and satisfy every gate; do
   not relax coverage, infer vintages, or splice venues.
2. **Ledger accumulation:** operate the #398 prospective shadow ledger and
   preserve its status report and immutable database evidence. Accumulate
    eligible, provenance-complete forecasts with exact matured outcomes at 2h,
    4h, 8h, and 16h, and account for all failures. No runtime database or
    status report was available for this closeout; #399 consequently records
    the ledger path, hash, and counts as unavailable. This closeout does not
    assert ledger coverage or maturity counts. The downstream replay gate
    requires all expected pairs to mature and no failed attempts.
3. **Parity replay:** after both prior gates pass, rerun the #399 readiness
   audit, attest production-policy parity, freeze the corpus/ledger hashes,
   policy identity, code revision, command, and evaluation cutoff, then execute
   the preregistered exact-key comparisons and publish metrics with
   dependence-aware uncertainty and family-wise multiplicity adjustment.
4. **Candidate confirmation:** only after valid parity evidence, freeze and
   hash the bounded candidate-family preregistration before any scoring. Retain
   every attempt and failure. A selected candidate would still require
   disjoint prospective confirmation for at least 30 calendar days and at
   least 200 exact mature pairs per supported horizon, plus human review;
   selection or shadow success does not automatically promote or alter
   production.

## Final evidence disposition

All four workstreams have auditable implementation/status artifacts, but the
empirical evidence chain did not reach a scoreable evaluation. The only
quantified corpus result is absence (0/32,136 target and 0/4,320 warm-up hours);
#399 has `metrics: null`; #400 has no scored candidates and a null winner. No
empirical accuracy conclusion—positive, negative, or inconclusive—is supported.
In particular, this closeout makes no accuracy-improvement claim and authorizes
no production configuration change.
