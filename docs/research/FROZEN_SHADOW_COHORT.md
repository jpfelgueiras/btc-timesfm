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

## Registered baseline capture

Shadow-store schema v7 adds append-only `parity_baseline_attempts`. During the
production shadow-monitoring step, `run_shadow` receives the exact `MarketData`
object already used to produce the public forecast and invokes
`benchmark_forecasts`: persistence, seasonal-naive 24h, drift 24h/7d, AR(1), and
EMA return. It records one row per baseline and 2h/4h/8h/16h target, including
origin/target, raw/final predictions, market pair/source, data lineage, a hash
of the complete OHLCV input window, production policy digest, code/model
identity, and any calculation failure. Shadow retries have distinct attempt
IDs; an existing attempt ID is immutable. These records are research evidence
only and do not feed public forecasts.

The cohort report now carries each production final prediction, raw model
predictions, actual target value when mature as-of the frozen cutoff, and a
content identity for that exact actual/source/target. The #411 replay helper
preserves failures, missing and unmatched attempts, checks exact keys and
lineage, and does not score when the cohort is empty, blocked, lacks the actual
identity, or training-only strongest-baseline selection is unavailable.

The last verified live cohort remains blocked as documented above: **zero
confirmatory origins and zero eligible pairs**. Future scoring requires newly
captured versioned origins that pass #419's cutoff/maturity/provenance gates,
complete baseline and production attempts for every exact key, exact matured
actual/source identity, a frozen training-only strongest-baseline selection,
and adequate #410 sample/block minima. The historical canonical path separately
requires an eligible licensed point-in-time corpus. No metric should be
backfilled from legacy rows.

The current machine-readable issue report is
[`ISSUE_411_BASELINE_CAPTURE_BLOCKED.json`](ISSUE_411_BASELINE_CAPTURE_BLOCKED.json).
Regenerate it with:

```bash
PYTHONPATH=src python -m btc_timesfm.research.production_parity_replay \
  --output docs/research/ISSUE_411_BASELINE_CAPTURE_BLOCKED.json
```

Capture rows can be exported and replayed (the example remains blocked until
eligible origins and matured actual evidence are present):

```bash
PYTHONPATH=src python -m btc_timesfm.research.shadow_deployment \
  --db .state/shadow_deployment.sqlite export-baselines \
  --out .state/parity-baseline-attempts.json
PYTHONPATH=src python -m btc_timesfm.research.parity_baselines \
  --cohort .state/frozen-cohort.json \
  --baseline-attempts .state/parity-baseline-attempts.json \
  --output .state/parity-replay-readiness.json
```

Production point attempts default to the champion point predictions embedded in
the cohort report. A separate `--production-attempts` file is available for
complete production attempt/failure ledgers. The replay emits exact matched
rows, unmatched attempts, and blockers; it does not score without an eligible
frozen cohort, matured actual/source identities, and training-only strongest
baseline selection. Each captured row carries both the frozen shadow-policy ID
used for exact cohort matching and a SHA-256 of the production policy object in
the experiment manifest; replay verifies both identities.

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
