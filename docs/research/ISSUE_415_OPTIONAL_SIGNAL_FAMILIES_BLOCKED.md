# Issue #415 — optional signal families: blocked disposition

**Disposition: blocked. No optional data-source family was audited, scored, or
added.** The machine-readable record is
[`ISSUE_415_OPTIONAL_SIGNAL_FAMILIES_BLOCKED.json`](ISSUE_415_OPTIONAL_SIGNAL_FAMILIES_BLOCKED.json).

## Evidence and reasons

Issue #411 currently reports zero eligible scored origins and zero eligible
scored pairs. All 48 timestamp-eligible rows are legacy-excluded, the newest
versioned forecast is immature, and no input-related error has been diagnosed.
There is also no available BTC/USD point-in-time corpus. These facts provide no
eligible outcome comparison and no diagnosed input failure for an optional
source family to address. Timestamp eligibility does not imply scored evidence.

Accordingly, no candidate family or source was examined or ranked. Every metric
is null, and there is no winner. No vendor was contacted and no unsupported data
was used. The recorded upstream evidence is
[`ISSUE_411_PARITY_BASELINES_BLOCKED.json`](ISSUE_411_PARITY_BASELINES_BLOCKED.json).

## Gates to reopen

Reconsider the issue only after all applicable gates are evidenced:

1. **Diagnosed input failure:** complete #411 with a reproducible input-related
   diagnosis tied to eligible production evidence.
2. **Source manifest:** identify exact source/vendor, venue, instrument/pair,
   fields and definitions, units, interval, timezone, and transformations.
3. **Point-in-time provenance:** establish event time and first-publication /
   availability time, with immutable revisions and original values sufficient
   to reconstruct what was knowable at each forecast origin.
4. **Licensing:** document acquisition, research-use, storage, retention, and
   derived-output rights for the exact source and fields.
5. **Operational feasibility:** measure outages and fallback behavior,
   freshness, costs, and end-to-end latency.
6. **Frozen ablation:** predeclare candidate features and family, missing-data
   handling, forecast integration, evaluation window, and scoring protocol
   before inspecting outcomes.
7. **Exact-pair inference:** compare only mature outcomes on exact shared
   `(origin_at, target_at, horizon)` keys; report exclusions and failures, and
   predeclare dependence-aware uncertainty plus family-wise multiplicity
   correction across all candidates, comparisons, and strata.

Until these gates pass, optional data-source feature work remains blocked and
must not influence production forecasts.
