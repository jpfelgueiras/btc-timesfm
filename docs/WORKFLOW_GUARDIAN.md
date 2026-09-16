# Workflow guardian watchdog

End-to-end monitoring for scheduled GitHub Actions workflows. The guardian
watches the production forecast, hourly dispatcher, site pages, weekly
optimizer, history audit and disaster-recovery drill workflows, classifies
failures, auto-retries transient failures once, detects missed schedules and
escalates persistent incidents into a tracked issue with this runbook linked.

## Watched workflows

| Workflow | File | Schedule | Freshness SLO source |
| --- | --- | --- | --- |
| forecast | `forecast.yml` | every 2h at :37 | `production_forecast` |
| hourly dispatcher | `hourly-forecast.yml` | every hour at :07 | `scheduled_run` |
| pages | `pages.yml` | every hour at :17 | `site_update` |
| optimizer | `optimize.yml` | weekly Sun 04:17 | — (9d run gap) |
| history audit | `history-audit.yml` | daily 05:17 | — (36h run gap) |
| disaster-recovery drill | `disaster-recovery-drill.yml` | weekly Mon 06:43 | — (9d run gap) |

## Failure classification

Failed runs are classified by scanning the conclusion, status and any available
failure text:

- **Transient** (auto-retried once with backoff): `rate_limit`,
  `runner_quiesced`, `network`, `upstream_api`.
- **Persistent** (escalated): `validation`, `code_error`, `data_error`,
  `unknown`.

Transient failures are retried once via `gh workflow run` after the configured
backoff (default 15 minutes). If the retry attempt also fails, the incident is
escalated as persistent.

## Missed-schedule detection

A workflow is treated as having missed its schedule when:

- the workflow's freshness SLO metric (if any) exceeds its critical tolerance
  — the freshness SLO is the source of truth; or
- no freshness metric exists and the latest workflow run is older than the
  configured cadence threshold.

## Escalation and deduplication

Persistent failures and missed schedules escalate into a GitHub issue labelled
`workflow-guardian`. Each workflow+category combination is a single tracked
incident: while an incident is open, repeat detections within the dedup window
(default 120 minutes) are suppressed and later detections annotate the existing
issue instead of creating duplicates. When the incident resolves (a scheduled
run succeeds or freshness recovers) the issue is commented with a recovery
message and the incident is closed.

## Running the guardian

```bash
python -m btc_timesfm.ops.workflow_guardian \
  --freshness-status freshness_slo_status.json \
  --state .state/workflow_guardian.json \
  --report workflow_guardian_report.json \
  --events workflow_guardian_events.jsonl \
  --emit-issue
```

Options:

- `--workflow NAME` — restrict to one or more watched workflows
- `--dedup-minutes`, `--retry-attempts`, `--retry-backoff-minutes` — override policy
- `--no-gh` — skip `gh` API calls (offline/CI dry runs)
- `--emit-issue` — create/annotate tracked GitHub issues
- `--now ISO` — deterministic timestamp for reproducible runs

Exit code is 1 when any new escalation is raised.

## Observability

Every classified failure, retry decision, escalation, dedup suppression and
recovery is appended to `workflow_guardian_events.jsonl`. The latest sweep is
summarized in `workflow_guardian_report.json` and durable incident/retry state
is stored in `.state/workflow_guardian.json`.