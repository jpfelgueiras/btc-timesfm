# Forecast-service SLOs and reliability dashboard

`service_slo.json` is the versioned, machine-readable source of truth for forecast-service availability SLOs. It complements `freshness_slo.json`: freshness continues to govern how current forecasts, site output, backups, and schedules must be; this SLO set measures outcome reliability and error-budget consumption.

## Domains and measurable objectives

The configuration requires one objective for each dashboard domain:

| Domain | Measured outcome | Default target | Window | Review |
| --- | --- | ---: | ---: | ---: |
| data | Market-data acquisition and validation | 99.0% | 30 days | 90 days |
| model | Forecast-model inference | 99.0% | 30 days | 90 days |
| storage | Durable forecast-history persistence | 99.9% | 30 days | 90 days |
| API | External dependency calls | 99.0% | 30 days | 90 days |
| publication | Forecast artifact and site publication | 99.0% | 30 days | 90 days |

Each objective declares its own `threshold_basis`, `min_events`, rolling `window_days`, and `review_cadence_days`. Targets are not evaluated until the minimum sample count is reached. Review threshold basis, target, and volume at least every 90 days and after material changes to workflow cadence or dependency behavior.

## Error-budget accounting

Events are normalized JSONL records with `timestamp`, `domain`, and terminal `status`. `success`, `healthy`, and `ok` count as successful; `failed`, `error`, and `unhealthy` consume budget. Other statuses do not enter the denominator.

For a rolling window containing `N` eligible events and target `T`:

```text
allowed_failures = N * (100 - T) / 100
remaining_error_budget = allowed_failures - failures
```

An SLO is `met` when failures do not exceed the allowed failures, `breached` when they do, and `insufficient_data` before `min_events`. This fractional accounting avoids hiding a breach in low-volume windows.

## Generate the provider-neutral dashboard

```bash
PYTHONPATH=src python -m btc_timesfm.ops.service_slo \
  --config service_slo.json \
  --events forecast_observability.jsonl \
  --json service_slo_dashboard.json \
  --markdown service_slo_dashboard.md
```

The JSON dashboard is suitable for a dashboard provider or API integration, while the Markdown artifact is suitable for CI summaries. The evaluator deliberately does not make provider calls. #218 and #219 can adapt their integrations to emit the normalized API events without changing SLO evaluation or error-budget logic.
