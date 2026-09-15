# Forecast freshness SLO monitoring and alerting

End-to-end SLO monitoring for production forecast freshness, public site updates, history backups, and scheduled workflow runs. The watchdog detects staleness, emits deduplicated alerts, records breaches/recoveries in metrics, and computes weekly SLO adherence.

## SLO configuration

The SLO is defined by `FreshnessSLOConfig` in `src/btc_timesfm/ops/freshness_slo.py`. Each metric specifies:

- `target_hours` — ideal freshness window
- `warning_tolerance_hours` — alert at warning level
- `critical_tolerance_hours` — alert at critical level

Default configuration:

| Metric | Description | Target | Warning | Critical |
| --- | --- | --- | --- | --- |
| `production_forecast` | Forecast within N hours of scheduled hour | 1h | 2h | 3h |
| `site_update` | Public site generated-at within N hours of completed forecast | 1h | 1.5h | 2h |
| `history_backup` | Backup newer than N hours | 12h | 20h | 24h |
| `scheduled_run` | Scheduled workflow ran within N hours | 1h | 1.5h | 2h |

Configuration can be overridden by writing a JSON file:

```json
{
  "schema_version": 1,
  "metrics": [...],
  "backup_max_age_hours": 24.0,
  "scheduled_run_max_gap_hours": 2.0,
  "alert_dedup_minutes": 60
}
```

## Running the watchdog

```bash
python -m btc_timesfm.ops.freshness_watchdog \
  --history-db .state/forecast_state.json \
  --site-data site/data.json \
  --backup-assets assets.json \
  --config freshness_slo.json \
  --state .state/freshness_watchdog.json \
  --status freshness_slo_status.json
```

Options:

- `--config` — path to SLO config JSON (defaults to built-in production thresholds)
- `--history-db` — path to forecast state JSON with `latest_close_at` timestamps
- `--site-data` — path to site `data.json` containing `generated_at`
- `--backup-assets` — path to a JSON array of backup release assets
- `--workflow` — workflow filename to check (default: `forecast.yml`)
- `--no-gh` — skip GitHub API calls (useful for offline testing)
- `--emit-issue` — file GitHub issues for critical breaches

Exit code is 1 if any breaches are detected.

## What the watchdog checks

### Stale forecast
Extracts the latest `latest_close_at` from the durable forecast state. If the age exceeds the critical tolerance, a critical SLO breach is raised.

### Stale site
Reads `site/data.json` and checks the `generated_at` timestamp. If older than the critical tolerance, a critical breach is raised.

### Missed backup
Scans release assets matching `forecast_history.backup-*.sqlite.gz`. If the newest backup is older than the critical tolerance, a critical breach is raised.

### Missed scheduled run
Calls `gh api` to fetch recent workflow runs for the configured workflow. If no scheduled run has completed within the critical tolerance, a critical breach is raised.

## Alert deduplication

Alerts are deduplicated per metric+severity combination using a configurable cooldown period (`alert_dedup_minutes`, default 60 minutes). The deduplication state is persisted in `.state/freshness_watchdog.json`. Once a breach is resolved (metric recovers), the dedup state for that metric is cleared so the next breach will alert again.

Each alert includes:
- Severity level (`warning` or `critical`)
- Metric name and measured value
- Runbook link to this document
- Recovery/resolution message when a breach is cleared

## Metrics recording

Every check is recorded in `freshness_slo_events.jsonl` with events:
- `check_ok` — metric is within SLO
- `breach` — metric exceeded tolerance
- `recovery` — previously breached metric is now healthy

Breach/recovery counts are included in the weekly performance dashboard summary via the standard `performance_dashboard.py` module.

## Weekly SLO adherence

The dashboard computes adherence as:

```
adherence = (total_checks - breaches) / total_checks * 100%
```

The adherence percentage is reported per metric and overall for the 7-day rolling window. Include it in the performance dashboard by pointing it at the SLO events log:

```bash
python -m btc_timesfm.research.performance_dashboard \
  --db .state/forecast_history.sqlite \
  --slo-metrics-log freshness_slo_events.jsonl \
  --json performance_dashboard.json \
  --markdown performance_dashboard.md \
  --html performance_dashboard.html
```

The `freshness_slo` key in the JSON report and the "Weekly freshness SLO adherence" section in the markdown/HTML output are only included when `--slo-metrics-log` is supplied. Run `compute_weekly_slo_adherence(metrics_log_path)` directly to get the machine-readable adherence summary (overall plus per-metric checks, breaches and adherence percentage).

## Alert channels

- **Status file**: `freshness_slo_status.json` — machine-readable alert payload
- **JSONL events**: `freshness_slo_events.jsonl` — append-only metric log
- **GitHub Issues**: filed automatically for critical breaches when `--emit-issue` is used
- **stdout**: JSON payload printed for CI log capture

## Observability patterns

This module follows the same patterns as:
- `ops/observability.py` — structured event logging, run identification
- `ops/pipeline_health.py` — circuit breakers, webhook notifications, publication gates
- `x/x_post_registry.py` — deduplication, idempotency, state persistence
