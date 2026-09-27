# Shadow deployment

Promise-policy-approved research challengers can run live next to the production
champion in **shadow mode**: they issue real forecasts on the same market data,
but those forecasts are persisted separately and never reach the public
`forecast.json`, the X post, or the production history store. Only when a
challenger's matured live outcomes have satisfied the configured observation
window and minimum sample requirements does promotion even become *possible* —
and even then it still requires the normal optimizer/promotion-policy review and
code-review/CI path. Nothing in shadow mode ever changes production output or
parameters automatically.

## Model

`src/btc_timesfm/research/shadow_deployment.py` provides:

- `ShadowStore` — a durable SQLite store (same migration/rollback conventions as
  `experiment_registry.py`) holding four bounded tables keyed by
  `configuration_id`:
  - `configurations` — approved challengers and the production champion
    (`role`, `approval_status`, stable `configuration_id`);
  - `shadow_forecasts` — one row per `(configuration_id, origin_at)`,
    first-write-wins so manual reruns are idempotent; schema version 3 introduced
    immutable provenance JSON (forecast schema version 1) containing ledger
    version, the production runtime experiment manifest (model/package, code,
    production data, and production configuration identity), the registered
    row-configuration fingerprint, and shadow-policy identity. The manifest
    describes the production runtime that generated the shared input; the
    row-configuration records the effective champion or challenger parameters
    actually used for this row;
  - `shadow_outcomes` — matured per-horizon outcomes filled once the exact
    target candle exists.
- `ShadowPolicy` — configurable requirements before promotion can be considered:
  `minimum_live_samples` and `observation_window_days` (defaults 32 and 30).
- `run_shadow` — persists the champion row by **reusing the public production
  predictions untouched**, then reweights only the approved challenger
  configurations and persists their predictions separately.
- `mature_shadow_outcomes` — fills outcomes from the same market data used for
  the champion.
- `build_shadow_status` / `render_summary` — Actions-friendly status report.
- `ShadowStore.evidence_report()` — forecast/horizon coverage, missingness,
  exact-target maturity, failures, and version counts. Rows without both a data
  lineage ID and versioned provenance are explicitly counted as legacy and
  excluded from its confirmatory pair denominator.
- `shadow_failures` — durable, idempotent failure records so a bad challenger
  is observable without interrupting the champion or public publication.
- a CLI (`python -m btc_timesfm.research.shadow_deployment`).

## Isolation guarantees

- **Public output is byte-for-byte unchanged.** The champion shadow row copies
  the production snapshot's predictions rather than recomputing them, and
  challenger records live in their own tables. `run_shadow` reports
  `production_output_changed: false` and `production_parameters_touched: false`.
- **Separate, idempotent persistence.** Forecasts are keyed by
  `(configuration_id, origin_at)`; re-running the same origin inserts nothing.
- **Approval is required.** Only `role="challenger"` configurations with
  `approval_status="approved"` run; pending/rejected configurations are skipped.
- **Identical-origin and lineage evaluation.** The public snapshot's manifest
  `data_id` is required and recorded at the same `latest_close_at` origin;
  matured challenger outcomes are compared against the champion and persistence
  on exactly the set of origins that both configurations share, using the same
  deterministic paired-bootstrap method as the research pipeline.
- **Observable failure isolation.** A challenger prediction failure is persisted
  and reported, but never interrupts the champion, modifies a public forecast,
  changes weights, or affects publication.

## Workflow

```bash
# Register an approved research challenger (or point --parameters at a JSON file)
PYTHONPATH=src python -m btc_timesfm.research.shadow_deployment approve \
  --name longer_history --parameters challenger_params.json

# At every production forecast origin, run shadows alongside it
PYTHONPATH=src python -m btc_timesfm.research.shadow_deployment record \
  --production production_snapshot.json --actuals .state/shadow_actuals.json

# Mature outcomes as target candles appear
PYTHONPATH=src python -m btc_timesfm.research.shadow_deployment mature \
  --actuals .state/shadow_actuals.json

# Evaluate one challenger and render the status report
PYTHONPATH=src python -m btc_timesfm.research.shadow_deployment evaluate \
  --configuration-id <challenger-configuration-id>
PYTHONPATH=src python -m btc_timesfm.research.shadow_deployment report
```

The status report (`shadow_deployment_report.json` /
`shadow_deployment_summary.md`) lists every approved challenger with its live
sample count, observation window, champion/challenger/persistence MAE, and its
promotion gate (`eligible` / `blocked`) plus the unmet reasons. It is designed
to be appended to an Actions step summary exactly like the optimizer and
promotion-policy summaries.

Forecast schema version 1 is stored in each row's provenance. Database schema
version 6 is managed through ordered migrations and rejects unsupported newer
schemas without modification. Outcomes pair by `(configuration_id, origin_at,
horizon)` and mature only when the exact UTC target candle exists; duplicate
forecast and outcome writes are idempotent/write-once. No legacy row is assigned
retroactive provenance, and no evidence report changes production parameters.
The report validates supported forecast schema version 1 and complete model,
code, production experiment configuration, registered row-configuration
fingerprint, policy, and matching lineage identity before treating a forecast
as confirmatory. The registered fingerprint is recomputed from the store's
configuration name, role, and parameters, and must match the forecast row's
configuration ID. Failed attempts carry an
expected-horizon set and an optional caller-supplied `attempt_id`. Repeated
errors with the same ID are combined; omitted IDs produce unique attempts, so
separate executions at the same configuration/origin/stage remain distinct.
Migration snapshots and rollback restoration use SQLite's online backup API,
so committed WAL frames are included and journal-mode changes are unnecessary
for transactional migrations.

## Promotion gate

A challenger's decision is `eligible` only when **all** of these hold:

- at least `minimum_live_samples` shadow observations have matured;
- the observation window spans at least `observation_window_days`;
- paired evidence shows the challenger is not significantly worse than the
  champion;
- paired evidence shows a (statistically supported) edge over the champion;
- paired evidence shows the challenger is not significantly worse than
  persistence.

Any unmet requirement keeps the decision at `blocked` with
`requirement_not_met:<check>` reasons. `eligible` authorizes nothing by itself:
promotion still requires the formal promotion policy, champion-vs-challenger
review, and the normal production change process.

## Retention

The shadow store is a single SQLite file (`.state/shadow_deployment.sqlite` by
default), bounded by its `(configuration_id, origin_at)` primary keys and one
outcome row per matured `(configuration_id, origin_at, horizon)`. Like the
experiment registry and production forecast history, the database is intended
to be kept as a compressed GitHub Release asset, which keeps it
Actions-compatible.

The scheduled workflow permits an empty store only when no history Release has
ever been established. Once that Release exists, failure to download or decompress
`shadow_deployment.sqlite.gz` stops the run; it must not initialize a replacement
ledger. Each successful origin records the exact UTC target timestamp for every
horizon in provenance. Status reports include store/forecast schema versions,
database SHA-256, first/latest origins, provenance coverage, missing and matured
pairs, and failure counts.

Back up the complete SQLite file using SQLite's online backup API (which
includes committed WAL frames) or after closing the store; retain it as a compressed release asset alongside the production history
asset. Reproduction uses the persisted forecast predictions and provenance, not
a regenerated current configuration. The operational `report` command emits
coverage/version/maturity evidence for inspection; retention and restore
procedures follow `HISTORY_BACKUP.md`.

The independent-backup manifest supports both `forecast_history` and
`shadow_deployment` database types. Production uploads the v6 shadow archive to
`${HISTORY_INDEPENDENT_BACKUP_URI}.shadow_deployment`; the monitor verifies its
schema, checksum, row counts, and age, and the recovery drill restores it into
scratch storage and verifies it without touching production state. Shadow
recovery receipts are recorded separately from production-history receipts.
