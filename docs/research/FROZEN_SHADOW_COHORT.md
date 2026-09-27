# Frozen prospective shadow cohort

`ShadowStore.frozen_cohort_report(origin_cutoff_at=..., evaluation_as_of=...)`
builds a read-only cohort summary from the current shadow ledger. Both timestamps
must be timezone-aware. The origin cutoff must be at least 16 hours before the
evaluation-as-of time; every included origin must also be at least 16 hours old.
Pairs use the stored configuration/origin/horizon identity and exact UTC target
candle, and carry the forecast, policy, and data-lineage identities into the
cohort hash. Failures at eligible origins remain in the denominator. Newer and
otherwise right-censored forecasts are reported separately.

The report records database, code, and cohort hashes. Preserve the report as an
immutable evidence artifact and preregister both cutoffs before examining
outcomes. `_frozen_cohort_blocker` validates cohort readiness independently of
the live all-rows #399 ledger gate. Passing cohort readiness establishes evidence
completeness only; it computes no forecast-quality metrics and is not a skill
claim. This prospective track does not satisfy the blocked historical corpus
requirement in #409.
