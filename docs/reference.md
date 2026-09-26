# Configuration, commands, and persisted state reference

This page inventories user-facing configuration and operational state from the
runtime, CLI parsers, `pyproject.toml`, and GitHub Actions workflows. `BTC_*`
settings used only by research or evaluation tools are not production controls.
Defaults below describe this checkout; workflow dispatch inputs and repository
variables/secrets can override them. Never put secret values in documentation,
logs, reports, or issue text.

## Runtime and workflow configuration

### Production and operational settings

| Name | Component / default | Required and validation | Scope |
|---|---|---|---|
| `BTC_FORECAST_HISTORY_DB` | Forecast API history; `.state/forecast_history.sqlite` | Optional; path | API service |
| `BTC_FORECAST_API_KEYS` | API authentication; empty | Optional comma-separated non-empty keys; requests need a configured valid key | API service |
| `BTC_FORECAST_FINGERPRINT_SECRET` | API fingerprinting; unset | Optional secret; never expose its value | API service |
| `BTC_PIPELINE_HEALTH_STATE` | API health state; `.state/pipeline_health.json` | Optional path | API service |
| `BTC_FORECAST_API_AUDIT_LOG` | API read audit; `.state/forecast_api_reads.jsonl` | Optional path | API service |
| `BTC_FORECAST_API_RATE_LIMIT` / `BTC_FORECAST_API_RATE_WINDOW_SECONDS` | API rate limit; `60` / `60` | Positive integers; invalid values fail configuration | API service |
| `BTC_FORECAST_API_STALE_AFTER_SECONDS` | API freshness; `10800` | Positive integer | API service |
| `BTC_FORECAST_API_AUDIT_MAX_BYTES` / `BTC_FORECAST_API_AUDIT_BACKUPS` / `BTC_FORECAST_API_AUDIT_MAX_AGE_DAYS` | API audit retention; `10000000` / `3` / `30` | Positive integer limits | API service |
| `BTC_RUN_ID` | Observability run identity; generated when absent | Optional non-empty identifier; falls back to GitHub run identity | Workflows and local operations |
| `BTC_X_CIRCUIT_COOLDOWN_MINUTES` | X circuit breaker; implementation default | Optional integer, must be positive | Production publication |
| `X_COOKIES_JSON` | Twikit X authentication; unset | Secret JSON cookie object; required only to publish, parsed/validated before use | Production X posting |
| `PIPELINE_HEALTH_WEBHOOK_URL` | Optional pipeline notification; unset | Optional URL; sending is best-effort | Production operations |
| `GITHUB_REPOSITORY`, `GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT`, `GITHUB_SHA`, `GIT_COMMIT`, `GITHUB_EVENT_NAME`, `GITHUB_OUTPUT`, `GITHUB_STEP_SUMMARY` | GitHub Actions context | Supplied by Actions where applicable; local runs may omit them | Workflow integration |
| `FORECAST_STATE_PATH` | Schedule guard; module state path | Optional path override | Workflow integration |
| `X_POST_REGISTRY_PATH` | Schedule guard; module state path | Optional path override | Workflow integration |
| `BTC_OPTIONAL_SOURCE_RETENTION_HOURS` / `BTC_OPTIONAL_SOURCE_RETENTION_MAX_RECORDS` | Optional-source state retention; implementation defaults | Optional positive integer limits | Production data-state retention |
| `BTC_DATA_INTERVAL_SECONDS`, `BTC_DATA_MIN_CANDLES`, `BTC_DATA_MAX_STALENESS_MINUTES`, `BTC_DATA_FUTURE_TOLERANCE_SECONDS`, `BTC_DATA_MAX_HOURLY_RETURN_PCT`, `BTC_DATA_MAX_CANDLE_RANGE_PCT`, `BTC_DATA_MAX_VOLUME_MEDIAN_MULTIPLIER`, `BTC_DATA_VOLUME_LOOKBACK`, `BTC_DATA_VOLUME_MIN_HISTORY` | Market-data validation; defaults in `MarketDataValidationConfig` | Optional numeric/integer bounds; positive counts/intervals and valid thresholds required | Production data validation |
| `BTC_GAP_ALLOWED_MISSING_CANDLES`, `BTC_GAP_FILL_MAX_CANDLES`, `BTC_GAP_COMPARE_CANDLES`, `BTC_GAP_MIN_OVERLAP`, `BTC_GAP_DIVERGENCE_THRESHOLD_PCT`, `BTC_GAP_REPORT_LIMIT` | Gap detection; defaults in `GapDetectionConfig` | Optional numeric/integer controls; count and overlap limits validated | Production data validation |
| `BTC_PROVIDER_MAX_CLOSE_DIFF_PCT`, `BTC_PROVIDER_COMPARE_CANDLES`, `BTC_PROVIDER_MIN_OVERLAP`, `BTC_PROVIDER_VOLUME_CAP_MULTIPLIER` | Provider comparison; defaults in `ProviderSelectionConfig` | Optional comparison tolerances/counts, validated by provider configuration | Production data validation |
| `BTC_SOURCE_HEALTH_MAX_OPTIONAL_AGE_HOURS`, `BTC_SOURCE_HEALTH_MAX_MISSING_FEATURE_RATIO`, `BTC_SOURCE_HEALTH_QUARANTINE_ON_REVISION`, `BTC_SOURCE_HEALTH_QUARANTINE_ON_DISAGREEMENT` | Optional-feed health; defaults in `SourceHealthConfig` | Optional nonnegative age/ratio and boolean controls; malformed values rejected | Production optional-source handling |
| `BTC_DRIFT_ERROR_RECENT`, `BTC_DRIFT_ERROR_BASELINE`, `BTC_DRIFT_FEATURE_RECENT`, `BTC_DRIFT_FEATURE_BASELINE`, `BTC_DRIFT_WARNING_SHIFT_Z`, `BTC_DRIFT_SEVERE_SHIFT_Z`, `BTC_DRIFT_WARNING_KS`, `BTC_DRIFT_SEVERE_KS`, `BTC_DRIFT_WARNING_DIRECTION_DROP`, `BTC_DRIFT_SEVERE_DIRECTION_DROP`, `BTC_DRIFT_WARNING_ADAPTIVE_CONFIDENCE`, `BTC_DRIFT_SEVERE_ADAPTIVE_CONFIDENCE` | Drift detection; defaults `8`, `24`, `12`, `36`, `2.0`, `3.5`, `0.35`, `0.55`, `0.15`, `0.25`, `0.50`, `0.0` | Optional typed thresholds; lower bounds/clamps and severe-vs-warning ordering validation apply | Production monitoring; thresholds are recorded with run provenance |

Market-data source credentials and source-specific validation/health thresholds
are optional provider configuration; missing or invalid optional feeds are
handled through the documented validation, freshness, quarantine, and fallback
path. Source credential names are defined in `data/market_data_sources.py`;
values must be kept in the workflow secret store or local environment, never
checked in. Production uses the configured provider selection, not research
feature switches.

### Research-only environment controls

These are opt-in tuning inputs, not production deployment configuration. Their
parsers convert to the stated type; module-specific bounds and minimum sample
requirements apply, and malformed numeric values fail at startup.

| Names | Component / defaults | Scope |
|---|---|---|
| `BTC_ADAPTIVE_HISTORY_LIMIT` | Adaptive weighting; `200` | Research/evaluation; history cap |
| `BTC_CONDITIONAL_VOL_CUTS`, `BTC_CONDITIONAL_COVERAGE_TOLERANCE` | Conditional calibration; unset (built-in cuts/tolerance) | Research calibration |
| `BTC_INTERVAL_TARGET_COVERAGE`, `BTC_CONFORMAL_HISTORY_LIMIT`, `BTC_CONFORMAL_MIN_SAMPLES` | Conformal calibration; `0.80`, `200`, `20` | Research/evaluation |
| `BTC_CORRELATION_HISTORY_LIMIT`, `BTC_CORRELATION_MIN_SAMPLES`, `BTC_CORRELATION_FULL_SAMPLES`, `BTC_CORRELATION_PENALTY_STRENGTH`, `BTC_CORRELATION_MAX_BLEND` | Correlation weighting; `120`, `12`, `36`, `0.55`, `0.70` | Research/evaluation |
| `BTC_UNCERTAINTY_HISTORY_LIMIT`, `BTC_UNCERTAINTY_TARGET_COVERAGE`, `BTC_UNCERTAINTY_MIN_SAMPLES`, `BTC_UNCERTAINTY_FULL_SAMPLES`, `BTC_UNCERTAINTY_PENALTY_STRENGTH`, `BTC_UNCERTAINTY_MAX_BLEND`, `BTC_UNCERTAINTY_OVERCONFIDENCE_GAP`, `BTC_UNCERTAINTY_DIRECTION_WEIGHT`, `BTC_UNCERTAINTY_MAX_DOMINANCE_RATIO`, `BTC_UNCERTAINTY_MIN_EVIDENCE_SAMPLES` | Uncertainty weighting; `200`, `0.80`, `6`, `24`, `0.60`, `0.70`, `0.15`, `0.50`, `2.0`, `32` | Research/evaluation |
| `BTC_GBDT_ESTIMATORS`, `BTC_GBDT_MAX_DEPTH`, `BTC_GBDT_MIN_SAMPLES_LEAF`, `BTC_GBDT_LEARNING_RATE`, `BTC_ELASTICNET_ALPHA`, `BTC_ELASTICNET_L1_RATIO`, `BTC_ELASTICNET_ITERATIONS`, `BTC_ELASTICNET_TOL` | Independent model evaluation; `40`, `2`, `8`, `0.05`, `0.6`, `0.6`, `200`, `1e-4` | Research challenger only |
| `BTC_RIDGE_MIN_TRAIN_SAMPLES`, `BTC_RIDGE_ALPHA`, `BTC_RIDGE_INITIAL_PRIOR_WEIGHT` | Diversified ridge candidate; `96`, `8.0`, `0.08` | Experimental candidate; disabled in production unless explicitly enabled and approved |
| `BTC_ENABLE_DIVERSIFIED_MODEL`, `BTC_ENABLE_INDEPENDENT_MODELS` | Experimental models; false | Explicit production opt-in gates; truth values are `1`, `true`, `yes`, or `on`; promotion still requires review | Gated model rollout |
| `BTC_RUN_REAL_TIMESFM` | Real-model test switch; false | Test-only; enables expensive integration tests | Development/CI |

The individual research workflow files may define additional run-local flags and
numeric parameters. Those workflow inputs control their named experiment and do
not implicitly alter production forecasts. See each `.github/workflows/*.yml`
and the matching CLI `--help` for the precise parser-supported inputs.

### GitHub Actions settings and secrets

| Setting | Component / default | Required and validation | Scope |
|---|---|---|---|
| `HISTORY_RELEASE_TAG` | Durable-history release; `forecast-history-v1` | Workflow constant | Production storage |
| `HISTORY_BACKUP_KEEP` | Versioned rollback generations; `7` | Positive count | Production storage |
| `HISTORY_BACKUP_MAX_BYTES` | Per compressed archive cap; `52428800` (50 MiB) | Positive byte count | Production storage |
| `HISTORY_BACKUP_MAX_TOTAL_BYTES` | Versioned-generation total cap; `262144000` (250 MiB) | Positive byte count | Production storage |
| `HISTORY_INDEPENDENT_BACKUP_URI` | S3-compatible destination; unset in this environment | Repository variable required for the production publish and independent monitor/drill; URI must be valid and independently administered | Backup setup outstanding |
| `HISTORY_BACKUP_AWS_REGION` | AWS CLI region; unset in this environment | Repository variable required for independent S3 operations | Backup setup outstanding |
| `HISTORY_BACKUP_AWS_ACCESS_KEY_ID`, `HISTORY_BACKUP_AWS_SECRET_ACCESS_KEY` | Independent backup AWS authentication | Repository secrets required for S3 read/write/restore | Backup setup outstanding |
| `X_COOKIES_JSON` | X publishing authentication | Repository secret required only when publishing to X | Optional publication |
| `PIPELINE_HEALTH_WEBHOOK_URL` | Health alert notification | Optional repository secret | Optional notification |
| `GH_TOKEN` | GitHub CLI API access | Workflow-scoped `github.token` with workflow permissions; not a manually configured secret | Actions integration |

**Independent S3 backup is not configured here.** The destination URI, region,
and AWS credentials are currently absent in this environment. Scheduled backup
monitor/drill checks therefore cannot be considered fulfilled. Configure the
repository variable and secrets above before relying on the independent copy.
The production publish intentionally fails closed rather than replacing the
canonical Release when this setup is missing. Secret names are listed for setup;
secret values are never displayed here.

## Real command-line entrypoints

Commands below are Python modules with a `__main__` parser in the package, or
repository scripts. This is not a list of importable library modules. Use
`PYTHONPATH=src` from a source checkout; `--help` is the authoritative list of
arguments and required options.

| Command | Purpose / scope |
|---|---|
| `python -m btc_timesfm.cli.btc_forecast` | Forecast generation; production workflow invokes the validated `python -m btc_timesfm.ops.validated_entrypoints forecast` wrapper |
| `python -m btc_timesfm.x.format_tweet`; `python -m btc_timesfm.x.post_to_x prepare|publish` | Render and optionally publish a forecast; X publishing requires the X secret and workflow gates |
| `python -m btc_timesfm.ops.schedule_guard` | Determine whether scheduled publication is due |
| `python -m btc_timesfm.ops.observability init|skip|finalize|summary|run-stage` | Workflow run and stage observability |
| `python -m btc_timesfm.ops.pipeline_health observe-forecast|observe-history|observe-x|gate|report|notify` | Health state and publication gates |
| `python -m btc_timesfm.history.history_store --db PATH init|verify|stats|summary|ingest-state|export` | Durable production history inspection and export |
| `python -m btc_timesfm.history.history_backup create|verify|restore|retention-plan` | Release archive creation, validation, recovery, pruning plan |
| `python -m btc_timesfm.history.independent_backup upload|restore|check|record-restore` | Independent object-store backup operations; require configured URI and AWS credentials |
| `python -m btc_timesfm.history.disaster_recovery` | Non-mutating restore validation/drill |
| `python -m btc_timesfm.history.history_audit` | History integrity/audit report |
| `python -m btc_timesfm.research.shadow_deployment --db PATH init|verify|stats|approve|record|mature|evaluate|report` | Isolated research challenger shadow state |
| `python -m btc_timesfm.research.experiment_registry --db PATH init|verify|stats|register|leaderboard` | Research experiment registry |
| `python -m btc_timesfm.ops.workflow_guardian`, `freshness_watchdog`, `gap_alerting`, `service_slo`, `promotion_policy`, `rollback_safeguards`, `release_state`, `optimizer_pr`, `flaky_tests`, `security_audit` | Operational CLIs; consult each module's `--help` for parser commands/options |
| `python -m btc_timesfm.web.static_site`, `python -m btc_timesfm.web.contract_validation` | Build and validate website output |
| `python -m btc_timesfm.forecasting.rolling_recalibration` | Research-oriented recalibration operation |
| `python -m btc_timesfm.research.<module>` | Parser-backed experiment/evaluation modules (for example `backtest`, `optimizer`, `canonical_benchmark`, `champion_challenger`, `performance_dashboard`, and ablations); inspect each module's `--help` |
| `python scripts/lint_workflows.py`, `python scripts/lint_action_pins.py`, `python scripts/check_required_checks.py`, `python scripts/pin_actions.py` | Repository maintenance and CI checks |

The Actions workflows are the canonical production invocations. In particular,
the hourly dispatcher runs `forecast.yml` with `post_to_x=false`; the scheduled
forecast workflow controls due checks, validation, publication, and state
persisting. Research CLIs and their environment parameters recommend/evaluate
changes; they do not promote them automatically.

## Persisted data and lifecycle

### Durable production history: SQLite schema v6

The forecast database uses `PRAGMA user_version` as the authoritative schema
version; `metadata.schema_version` mirrors it for diagnostics. Current version
is **6** (`history_migrations.CURRENT_SCHEMA_VERSION`). The `forecast-history-v1`
GitHub Release tag is a stable datastore product identifier, **not** a SQLite
schema number.

Core tables and keys:

| Table | Key and contents |
|---|---|
| `metadata` | `key` primary key, `value`; includes `schema_version` |
| `forecast_origins` | `origin_at` primary key; generated time, source/pair/price, regime/features, first/last seen; v3 adds `experiment_run_id`, `configuration_id`, `experiment_manifest_json`; v5 adds `multi_horizon_coherence_json` |
| `forecast_predictions` | `(origin_at, model_name, horizon_hours)` primary key; exact `target_at`, prediction and interval fields, and nullable matured actual/error/direction/coverage fields; origin foreign key cascades on delete |
| `schema_migrations` | `version` primary key, migration name and applied time; contiguous applied versions 1–6 |
| `drift_events` | Unique `(evaluation_origin_at, signal_key, severity)`; warning/severe event metrics and optional model/horizon/feature identifiers |

Migrations 1–6 add initial history, migration audit, experiment manifests,
drift events, multi-horizon coherence, and API ordering index respectively.
Supported older schemas upgrade automatically with ordered migrations inside a
transaction; existing DBs get a byte-for-byte `.pre-migration-vN.bak` copy. A
failed migration restores the copy; newer unsupported schemas are rejected
without modification. Schema version and migration audit are validated along
with SQLite integrity and foreign keys.

Forecast origins and predictions are durable, write-once historical forecasts:
reruns do not replace the first prediction/manifest for a key. Outcome
maturation fills nullable outcome fields only when the candle at the prediction's
exact `target_at` timestamp is available. Outcomes are then write-once; future
observations do not shift the target to a nearby timestamp. This preserves
walk-forward origin/outcome separation.

### Release assets, artifacts, and access

The production workflow maintains release tag `forecast-history-v1`. It contains
`forecast_history.sqlite.gz` (canonical DB), `forecast_history.csv.gz` (analysis
export), `forecast_history.previous.sqlite.gz` (prior canonical alias), bounded
`forecast_history.backup-YYYYMMDDTHHMMSSZ-RUN_ID.sqlite.gz` rollback generations,
plus state assets described below. The public GitHub Pages website is separate.
The GitHub Release is **public**, not private; do not store secrets or private
data in its assets. Asset visibility follows the repository's Release
visibility. Workflow run artifacts are also a separate store and have their own
retention (forecast artifacts: 7 days; monitor/drill reports: 90 days).

For later publishes, the workflow downloads/verifies the existing canonical
database, migrates and forecasts, uploads and re-downloads a verified prior
generation, and only then replaces canonical assets. It prunes only versioned
generation assets after successful publication, to at most 7 and 250 MiB (new
generation cap 50 MiB). The first successful publish has no prior generation.
Schema migrations do not change the release tag.

The independent S3-compatible backup is separate from Release retention and
cannot be inferred from the Release copy. It stores immutable SHA-256-named
archive generations and a manifest pointer, verifies downloaded data before
switching the pointer, and is monitored every 30 minutes with a 2-hour age gate;
weekly recovery drills target RPO 3 hours/RTO 1 hour. These are workflow design
targets, not evidence of configured service. URI, AWS region, and both AWS
secrets remain outstanding repository setup in this environment.

### Other persisted state

| State | Default/location and key semantics | Lifecycle / visibility |
|---|---|---|
| Forecast run state | `.state/previous_forecast.json` | Actions cache keyed by run, restored by prefix; not canonical forecast history |
| X post registry | `.state/x_post_registry.json` | Durable Release state; deduplicates scheduled posts and prevents duplicate publication |
| Pipeline health | `.state/pipeline_health.json` | Durable Release state; updated after forecast/publication observations |
| Optional source retention | `.state/optional_source_retention.json` | Small bounded optional-source freshness/quarantine state; persisted on Release |
| Optional source health | `.state/source_health_state.json` | Per-source/origin content digests used to detect revisions; workspace state unless explicitly persisted by an invocation |
| Shadow deployment | `.state/shadow_deployment.sqlite` (workflow also emits a gzip Release asset) | Separate SQLite tables: configurations keyed by configuration ID; forecasts by `(configuration_id, origin_at)`; outcomes by `(configuration_id, origin_at, horizon)`; failures idempotent. Challenger outcomes mature against exact target candles. Shadow records never alter public forecast or production history |
| Experiment registry | Caller-selected SQLite path | Research-only registry; separate from durable production forecast history |
| API read audit | `.state/forecast_api_reads.jsonl` | Rotated/age-limited local service audit; not forecast history |
| Per-run reports | Workspace and Actions artifact | Forecast, validation, drift, health and shadow reports; ephemeral under artifact retention |

Optional feeds are fallible; state describing source health/retention supports
validation and fallback and does not mean optional data are required. Shadow
state is intentionally separate; its presence does not represent production
approval or promotion.

## Related references

- [History schema compatibility](HISTORY_SCHEMA.md)
- [History backup and recovery](HISTORY_BACKUP.md)
- [Disaster recovery](DISASTER_RECOVERY.md)
- [Shadow deployment](SHADOW_DEPLOYMENT.md)
- [Workflow inventory](CI_WORKFLOWS.md)
- [Forecast API service](FORECAST_API_SERVICE.md)
