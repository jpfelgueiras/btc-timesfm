# Forecast pipeline observability

Production forecast runs write two machine-readable files:

- `forecast_observability.json` — current run snapshot with status, identifiers, counters and stage timings.
- `forecast_observability.jsonl` — append-only structured event stream suitable for later ingestion into a metrics/logging system.

Drift-aware runs additionally write `drift_report.json` and `drift_summary.md`; the former is the complete reproducible drift decision and the latter is appended directly to the GitHub Actions summary.

## Correlation identifiers

GitHub Actions runs use `github-<run_id>-<attempt>` as the stable pipeline run identifier. Once the forecast experiment manifest is created, its `run_id` is attached as `experiment_id` to the observability snapshot and all later events. `configuration_id` and `data_id` are also recorded as metadata.

## Stages

Production instrumentation exposes the following core stages:

- `market_data_fetch` — redundant provider fetch and provider-side validation.
- `market_data_validation` — selected provider validation outcome, fallback status and comparison status.
- `history_open`, `history_bootstrap_persistence`, `history_read`, `history_metrics`, `history_persistence`, `history_verify` — durable history lifecycle.
- `drift_detection` — rolling matured-error and observed-feature drift evaluation.
- `drift_persistence` — durable persistence of warning/severe drift events.
- `model_load` — TimesFM loading.
- `model_inference` — ensemble forecast inference.
- `forecast_pipeline` — end-to-end forecast application stage.
- `x_post` — X/Twikit publication, timed in the Actions workflow.

Each stage has UTC start/end timestamps, elapsed milliseconds and a status. Failed stages include exception type and message. Drift evaluation also emits a structured `drift_evaluated` event with severity, adaptive confidence, event count and fallback mode.

## Counters

The snapshot maintains counters for:

- `skips` — scheduled runs intentionally skipped by the candle-gap guard.
- `failures` — failed instrumented stages.
- `fallbacks` — production market-data failovers.
- `data_quality_events` — selected-provider warnings or unhealthy-primary validation events.
- `drift_warnings` — production runs whose overall drift state is warning.
- `drift_severe` — production runs whose overall drift state is severe.
- `successful_posts` — successful X publications.

## GitHub Actions

The forecast workflow initializes observability before the schedule guard, so even skipped runs get a terminal status and run identifier. An `always()` finalizer marks workflow failures and appends a concise table of timings/counters to the GitHub Actions job summary. Full observability JSON/JSONL files plus drift report/summary files are uploaded with forecast artifacts for completed forecast runs.

The module uses only the Python standard library so initialization and failure finalization do not depend on the TimesFM environment being installed successfully.
