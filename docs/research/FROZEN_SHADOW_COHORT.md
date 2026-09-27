# Frozen prospective shadow cohort

`ShadowStore.frozen_cohort_report(origin_cutoff_at=..., evaluation_as_of=...)`
builds a read-only cohort summary from the current shadow ledger. Both timestamps
must be timezone-aware. The origin cutoff must be at least 16 hours before the
evaluation-as-of time; every included origin must also be at least 16 hours old.
Pairs use the stored configuration/origin/horizon identity and exact UTC target
candle, and carry the champion configuration, policy, model revision/package,
forecast, and data-lineage identities into the cohort hash. The report is read
from one consistent SQLite backup serialization; `database_sha256` hashes those
exact snapshot bytes. Outcomes and failures observed after `evaluation_as_of`
are ineligible. Failures at eligible origins remain in the denominator. Newer and
otherwise right-censored forecasts are reported separately.

The report records database, code, and cohort hashes. Preserve the report as an
immutable evidence artifact and preregister both cutoffs before examining
outcomes. `_frozen_cohort_blocker` validates cohort readiness independently of
the live all-rows #399 ledger gate. Passing cohort readiness establishes evidence
completeness only; it computes no forecast-quality metrics and is not a skill
claim. This prospective track does not satisfy the blocked historical corpus
requirement in #409.

## Sanitized live-artifact verification

On 2026-09-27, the approved `forecast-history-v1` shadow artifact was evaluated
with `origin_cutoff_at=2026-09-26T19:00:00Z` and
`evaluation_as_of=2026-09-27T11:00:00Z`. The workflow report's raw database SHA-256
matched the restored artifact (`e802d233bb0ed99d0ad13317a16c1a747ca792cd521fa74a9a4312d40777e7b8`);
the consistent backup serialization used by this report hashed to
`b102f14db535264ecc2474a3b338c929abea4c39616f233a5701611c9300f57f`, with cohort
hash `462287eb433a27c16e4f25360d9ee40e5dda2fabdced13e5390859a055f7bbb8` and
code hash `79a95fc3debd4134ab68cfc55795007a340f15a6eff2ef7212a875a9846c28fc`.

The sanitized counts were 54 total stored origins, 48 timestamp-eligible
pre-cutoff forecasts, all 48 excluded as legacy/unversioned, zero confirmatory
origins, zero expected/matured confirmatory pairs, six right-censored forecasts,
and three versioned-provenance blockers. Right-censored pairs by horizon were
2h: 2, 4h: 3, 8h: 3, and 16h: 6. The latest provenance-complete origin was
`2026-09-27T11:00:00Z`, outside the 16-hour mature cohort at that as-of.
Readiness was blocked for `versioned_provenance_mismatch` and
`no_confirmatory_versioned_forecasts_in_frozen_cohort`. No metrics were computed;
the SQLite artifact itself is not stored in the repository.
