# Issue #413 — constrained ensemble weighting blocked

## Disposition

**Blocked; no weighting evaluation performed.** The merged #411 report is
`blocked`, has `metric_status: not_computed_blocked`, and records null metrics,
zero confirmatory forecasts, zero expected pairs, and zero matured pairs. Its
last verified prospective snapshot reports 48 timestamp-eligible forecasts,
all 48 legacy-excluded, and the latest provenance-complete origin as immature.
The report's blockers are `versioned_provenance_mismatch` and
`no_confirmatory_versioned_forecasts_in_frozen_cohort`.

Accordingly, #411 supplies no powered ensemble-versus-persistence diagnosis.
There is no supported basis for choosing a directional weighting hypothesis or
candidate family. Selecting weights now would be post-hoc and untestable against
the unavailable historical corpus; no candidate was scored, no metric was
computed, and no winner was selected. Production forecasts and configuration
were not changed.

## Evidence provenance

All state and source digests below are copied from the #411 artifact; no
independent claims about underlying data are made here. #411 identifies its own
artifact path but does not provide a digest for that file.

| Source | Hash / value in #411 |
| --- | --- |
| Frozen #410 protocol digest | `protocol_sha256: 4da936d505df9a15cbd073ee85ee0e4ed2db483e6058f6b81ce163f4724bb4f0` |
| Frozen #410 protocol file | `file_sha256: 1b8c6705d53882a66c0ab1277cf2a63c845d00c2c7d0cc978c0c75d8121efd18` |
| #397 corpus audit | `sha256: e2bf0c671a5a4327c6852d02614a7d2e403ed12f8fe7c4362336c5e87d44a0c7` |
| Verified snapshot code | `code_sha256: 79a95fc3debd4134ab68cfc55795007a340f15a6eff2ef7212a875a9846c28fc` |
| Verified snapshot cohort | `cohort_sha256: 462287eb433a27c16e4f25360d9ee40e5dda2fabdced13e5390859a055f7bbb8` |
| Verified snapshot database | `database_sha256: b102f14db535264ecc2474a3b338c929abea4c39616f233a5701611c9300f57f` |

The complete machine-readable values and disposition are in
[`ISSUE_413_ENSEMBLE_WEIGHTING_BLOCKED.json`](ISSUE_413_ENSEMBLE_WEIGHTING_BLOCKED.json).

## Exact reopening and preregistration conditions

Do not score weighting variants unless all of the following are satisfied and
archived before score access:

1. A subsequent #411 diagnosis, supported by its frozen evidence and hashes,
   establishes a powered ensemble-versus-persistence gap under the frozen
   #410 protocol. The diagnosis must meet its exact-pair and per-comparison
   sample/effective-block requirements; more elapsed time or legacy rows alone
   do not satisfy this gate.
2. Freeze and hash a bounded, exhaustive candidate family and directional
   weighting hypotheses motivated by that diagnosis. Register all comparisons,
   endpoints, strata, cutoffs, and stopping rules before scoring; account for
   every attempted, failed, blocked, and excluded candidate.
3. Fit weights only on nested, purged, training-only folds. Freeze each choice
   before the corresponding outer outcomes mature. Keep the untouched final
   holdout and prospective confirmation out of weight selection.
4. Score only exact shared `(origin_at, target_at, horizon)` pairs that meet
   #410 timestamp, maturity, outcome/vintage identity, and provenance rules.
   Exclude no failures silently and do not use unmatched, immature,
   legacy-excluded, or otherwise ineligible rows.
5. Apply the frozen #410 metrics, baseline comparisons, complete attempt
   accounting, dependence-aware paired moving-block inference, and family-wise
   adjustment. Archive the preregistration, hashes, evaluation manifest, and
   complete results for human review. Any protocol change requires a newly
   versioned and hashed contract before an independent evaluation.

The frozen protocol reference is the #410 `protocol_sha256` recorded above.
Until every reopening condition is met, this issue remains blocked and
production remains unchanged.
