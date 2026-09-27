# Issue #399 / #411 — production-parity replay readiness

## Current outcome: blocked

No replay metrics are produced or interpreted as a canonical skill result until
both the #397 corpus gate and #398 eligible, mature production-policy evidence
gate pass. The current #397 audit documents 0/32,136 target hours and 0/4,320
warm-up hours, with source provenance unavailable. The prospective #398 shadow
ledger has not yet accumulated eligible forecasts with matured outcomes. Policy
identity alone does not attest that the live execution wiring was production
parity.

The checked-in machine-readable report,
[`ISSUE_399_PRODUCTION_PARITY_BLOCKED.json`](ISSUE_399_PRODUCTION_PARITY_BLOCKED.json),
contains actionable blockers, evidence paths and hashes, `metrics: null`, and
explicitly denies canonical interpretation. It is a readiness audit, not a
forecast evaluation. Regenerate it from the repository root with:

```bash
PYTHONPATH=src python -m btc_timesfm.research.production_parity_replay \
  --corpus-audit docs/research/ISSUE_397_CANONICAL_BENCHMARK_AUDIT.json \
  --output docs/research/ISSUE_399_PRODUCTION_PARITY_BLOCKED.json
```

When an actual #398 `build_shadow_status` report is available, pass its JSON
with `--ledger PATH`. The accepted shape is the report's `schema_version: 1`, a
nonempty `policy_id`, named `champion.configuration_id`, and `evidence` with
`schema_version: 6`, `forecast_schema_version: 1`, `confirmatory_forecasts`,
`expected_pairs`, `matured_pairs`, `maturity_fraction`,
`missing_pairs_by_horizon` (`2h`, `4h`, `8h`, `16h`), `failures`,
`failed_attempt_pairs`, and `forecast_versions`. Counts must be exact and
reconcile (`expected_pairs = confirmatory_forecasts * 4 + failed_attempt_pairs`,
missing pairs equal expected minus matured, and version counts sum to
confirmatory forecasts). Replay readiness conservatively requires at least one
confirmatory forecast, all expected pairs mature, and no failed attempts. A
parity attestation is additionally required as top-level
`policy_parity_attested: true`, or `policy_parity_attestation: {"attested":
true, "policy_id": "..."}` scoped to the report's policy ID. An arbitrary
status string cannot qualify the ledger.

The corpus gate requires `status: ready_for_replay`, exact 32,136 target and
4,320 warm-up observations with no missing hours, zero gaps/duplicates/errors,
a named venue and USD pair, source file/SHA-256, and vintage/revision
provenance. It deliberately ignores `eligible_for_skill_comparison`, which the
#397 audit keeps false until this replay and validation have occurred. Neither
gate creates metrics or permits a canonical skill claim. Do not modify
production state to obtain evidence.

## Replay contract after gates pass

Freeze an immutable replay manifest with corpus and ledger SHA-256, code
revision, exact production policy/configuration identity, command, and fixed
evaluation cutoff. Compare the exact production policy against persistence,
seasonal-naive, and the strongest simple baseline on the intersection of exact
`(origin_at, target_at, horizon)` keys. Publish MAE, signed bias, direction
accuracy, sample count, and failures by horizon and regime; report unavailable
strata and failed forecasts explicitly rather than dropping them. Use paired
moving-block uncertainty to preserve temporal dependence and a predeclared
family-wise multiplicity adjustment spanning all comparisons and strata.

Do not infer missing values, stitch venues, substitute BTCUSDT for BTC/USD,
reuse immature outcomes, or call a blocked result a canonical skill estimate.
Once evidence is available, rerun the readiness command, validate each gate,
then execute and separately archive the frozen replay and its complete manifest.

## Issue #411 capture/readiness path

The #411 report uses the same audit implementation, validates the frozen #410
protocol schema and digest before readiness, and keeps canonical historical
corpus evidence separate from the frozen #419 prospective cohort. Generate it
with `--output docs/research/ISSUE_411_PARITY_BASELINES_BLOCKED.json`; provide
the immutable #419 report with `--frozen-cohort PATH` when available. A missing,
invalid, or blocked cohort is an explicit blocker. Passing readiness computes no
metrics and does not itself execute any models.

At the 2026-09-27 #419 snapshot, the prospective cohort had 48 timestamp-eligible
pre-cutoff forecasts, all 48 legacy/unversioned, zero confirmatory origins and
zero eligible pairs. The provenance-complete 11:00 UTC origin was not mature at
the frozen as-of time. It is excluded under #419; older mixed-version forecasts
must not be scored. The historical canonical source gate also remains blocked.
Keep metrics null until exact registered production captures and point-in-time
inputs satisfy the frozen protocol; prospective results never become canonical
historical results.
