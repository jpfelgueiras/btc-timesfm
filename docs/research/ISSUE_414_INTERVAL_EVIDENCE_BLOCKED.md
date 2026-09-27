# Issue #414 — interval evidence disposition

## Disposition: blocked; no interval claim

No interval scores, calibration fits, coverage estimates, widths, proper scores,
or calibration parameters were computed. There is no evidence-based diagnosis
that the production intervals are miscalibrated: the prerequisite #411
eligible, matured forecast lineage is absent, and the #409 canonical corpus is
blocked. This is an evidence-readiness disposition, not evidence for or against
interval calibration or probabilistic skill.

Point forecasts and their production behavior are unchanged. No interval
adjustment, point forecast change, performance metric, or model winner is
proposed or selected. Every interval metric is unavailable (`null`), rather
than zero.

## Evidence captured from current #411 report

The sole source for the snapshot facts and evidence hashes below is
[`ISSUE_411_PARITY_BASELINES_BLOCKED.json`](ISSUE_411_PARITY_BASELINES_BLOCKED.json).
That report records its latest verified prospective snapshot as of
`2026-09-27T11:00:00Z`:

- 48 timestamp-eligible forecasts; all 48 are legacy/unversioned and excluded.
- 0 confirmatory forecasts, 0 expected pairs, and 0 matured pairs.
- The latest provenance-complete origin, `2026-09-27T11:00:00Z`, was not mature
  for the cohort at that snapshot.
- Prospective and historical-canonical tracks are blocked and their metrics
  are null. The canonical corpus gate is blocked; the frozen prospective cohort
  is not ready.
- The recorded blockers include versioned-provenance mismatch and no
  confirmatory versioned forecasts in the frozen cohort.

Hashes are reproduced exactly from the #411 report; no new artifact, database,
code, corpus, or cohort hash is asserted here:

| #411 evidence | SHA-256 |
| --- | --- |
| #411 audit code | `79a95fc3debd4134ab68cfc55795007a340f15a6eff2ef7212a875a9846c28fc` |
| Snapshot database | `b102f14db535264ecc2474a3b338c929abea4c39616f233a5701611c9300f57f` |
| Last verified frozen-cohort content hash in snapshot | `462287eb433a27c16e4f25360d9ee40e5dda2fabdced13e5390859a055f7bbb8` |
| Frozen #410 protocol file | `1b8c6705d53882a66c0ab1277cf2a63c845d00c2c7d0cc978c0c75d8121efd18` |
| Frozen #410 protocol content | `4da936d505df9a15cbd073ee85ee0e4ed2db483e6058f6b81ce163f4724bb4f0` |
| #397 corpus audit | `e2bf0c671a5a4327c6852d02614a7d2e403ed12f8fe7c4362336c5e87d44a0c7` |

The #411 report has no cohort path/hash or ledger path/hash for this snapshot;
none is inferred. These hashes identify evidence referenced by #411 and do not
turn blocked or missing evidence into eligible observations.

## Why no interval claim is possible

Interval diagnosis requires observations with versioned, provenance-complete
production forecast lineage and exact, matured outcomes. The current #411
snapshot supplies no confirmatory forecast origins or eligible pairs. The
timestamp-eligible legacy rows cannot establish which forecast configuration
and interval-generation policy produced them. The newest complete origin has
not matured. Separately, the #409 corpus is blocked, so it cannot supply a
canonical historical evaluation. There is consequently no admissible sample on
which to estimate calibration, evaluate proper scores, or diagnose
miscalibration. Null metrics mean not evaluated, not a failed or successful
interval result.

## Exact re-open gates

Re-open interval evaluation only after all of these gates are met and the
evidence is frozen before outcome-score access:

1. **Eligible matured lineage (#411):** produce immutable, versioned
   production-policy forecast records with policy/configuration identity,
   point-in-time provenance, exact origin/target/horizon keys, recorded
   interval quantiles, and exact matured outcomes for the same vintage. The
   frozen cohort must report nonzero confirmatory origins and matured eligible
   pairs; legacy/unversioned rows remain excluded. Re-run #411 and preserve its
   report and source evidence hashes.
2. **Canonical corpus (#409):** resolve its documented readiness requirements
   and obtain an eligible point-in-time corpus audit with complete exact target
   and warm-up coverage, source/vintage provenance, and no unresolved gaps,
   duplicates, or errors. Keep historical-canonical and prospective evidence
   distinct.
3. **Frozen interval estimands and quantiles:** before scoring, freeze the
   interval-producing policy and quantile levels. For the current #410
   protocol, these are cumulative target log-return `q10`, `q50`, and `q90`;
   `L=q10` and `U=q90`, with finite ordered bounds. Do not fit or retune
   quantiles on evaluation/test outcomes. Any different quantiles or estimand
   require a separately versioned, hashed protocol frozen before score access.
4. **Prior-only calibration:** if calibration is proposed, fit its parameters
   using only data available before each evaluation origin (nested,
   walk-forward/prior-only fitting); record fit windows, input lineage, and
   parameter versions. Do not use evaluation, holdout, or D3 outcomes to fit,
   select, or revise calibration.
5. **Frozen scoring protocol:** register and hash the protocol, forecast
   candidates, exact pairing/outcome vintage, cutoffs, strata, complete test
   family, and analysis procedure before outcome-score access. Under frozen
   #410 v11-prospective-1.0.1, score exact matured pairs using per-quantile
   pinball loss and the primary 80% Winkler score, alongside 80% coverage and
   interval width; coverage alone is not a proper-score or skill claim. Preserve
   attempted/failed/abstained/missing denominators and apply its paired
   24-origin moving-block bootstrap and family-wise Holm adjustment to the
   entire registered family. Any changed method requires a new protocol
   version and digest before a new independent evaluation.
6. **Disjoint evaluation and D3:** reserve the chronological test/holdout
   disjoint from all calibration fitting and model/quantile selection. Keep the
   prospective D3 phase separate and untouched by training, calibration, or
   protocol changes; the frozen protocol requires at least 30 calendar days and
   200 mature exact pairs per horizon for D3. Do not reuse test or D3 outcomes
   for calibration or claim confirmation after tuning.

Passing readiness gates authorizes a separately frozen evaluation; it does not
pre-judge its metrics, establish miscalibration, or select a winner.
