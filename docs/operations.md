# Operations

This page is the operator map for scheduled forecasting and publication. Workflow
definitions are authoritative for mutable schedules and defaults; use the Actions
run summary, logs, and artifacts described below when diagnosing a run.

## Workflow schedule and interface

GitHub Actions cron expressions use UTC. Scheduled execution is best-effort;
GitHub may delay or drop cron events. The principal production and research
workflows are:

| Workflow (Actions display name) | Trigger and inputs | Effect and outputs |
| --- | --- | --- |
| `forecast.yml` — **BTC TimesFM 3 Forecast** | `schedule`: `37 */2 * * *` (every two hours at :37); manual dispatch input `post_to_x` is boolean, optional, default `false`. | Schedule guard may skip a tick if the latest completed candle is not due. Runs BTC forecast and attempts durable history publication; scheduled runs are eligible for guarded X publication. Manual runs do not post unless explicitly requested. Summary includes forecast, validation, history, backup, X, and pipeline health details. Artifact `btc-forecast-<run_id>`, 7 days, includes `forecast.json`, `tweet.txt`, X status, validation/source reports, drift and health reports, observability JSON/JSONL, shadow state/report, and applicable `.state` publication metadata. |
| `hourly-forecast.yml` — **Hourly BTC Forecast Dispatcher** | Schedule `7 * * * *` (hourly at :07), or manual dispatch with no inputs. | Dispatches `forecast.yml` on `main` with `post_to_x=false`; no forecast artifact of its own. It cancels an in-progress dispatcher in the same concurrency group. |
| `pages.yml` — **BTC Forecast GitHub Pages** | Hourly schedule `17 * * * *`; push to `main`; completion of **BTC TimesFM 3 Forecast** (success or failure); or manual dispatch, no inputs. | Builds strict MkDocs docs plus static forecast site from the canonical Release database and deploys Pages. The Pages deployment artifact is consumed by the deploy job. |
| `backtest.yml` — **BTC Forecast Backtest** | Manual dispatch only. `days` string defaults to `90`; `samples` string defaults to `60`. | Walk-forward historical evaluation. Artifact `btc-backtest-<run_id>`, 30 days: `backtest_report.json` and `market_data_validation.json`. |
| `optimize.yml` — **Weekly Forecast Optimizer** | Sunday schedule `17 4 * * 0` (04:17 UTC); manual dispatch. `days` defaults to `120`; `samples` defaults to `48`. | Bounded optimization, promotion-policy diagnostics, and champion/challenger report. Recommendation/evaluation only: it does not implicitly change production model configuration. Artifacts `btc-optimizer-<run_id>` and `btc-champion-challenger-<run_id>`, each 90 days, with reports, summaries, decisions, validation, and available health snapshot. |
| `independent-history-backup-monitor.yml` — **Independent forecast-history backup monitor** | Every 30 minutes (`*/30 * * * *`) or manual dispatch, no inputs. | Verifies independent archive/manifest and 2-hour maximum age. Artifact `independent-history-backup-monitor-<run_id>`, 90 days. Failure opens a deduplicated incident. |
| `disaster-recovery-drill.yml` — **Forecast history disaster-recovery drill** | Monday at 06:43 UTC (`43 6 * * 1`) or manual dispatch, no inputs. | Scratch-only restore and audit; records an independent restore receipt only on full success. Artifact `forecast-history-disaster-recovery`, 90 days. Failure opens a deduplicated issue. |

The schedule guard on the production workflow is separate from the cron:
`btc_timesfm.ops.schedule_guard` checks the last forecast close against the
latest completed UTC candle and skips scheduled events until at least one
completed candle hour has elapsed. Manual dispatches always proceed. The hourly
dispatcher dispatches a manual run with X disabled, so it can also cause hourly
forecasts; this is distinct from the forecast workflow's own two-hour scheduled
trigger at :37. See [forecast lifecycle](FORECAST_WORKS.md) and
[pipeline health](PIPELINE_HEALTH.md) for details.

To inspect a run, open **Actions → workflow → run**, read the step logs and
`GITHUB_STEP_SUMMARY`, and download its named artifact before it expires. From a
checkout, `gh run list --workflow forecast.yml` and
`gh run view RUN_ID --log` show run state/logs. Workflow artifacts are temporary
diagnostic outputs, not durable history. The `forecast-history-v1` Release is
machine-managed persistent state; do not edit its assets manually.

## State and publication boundaries

- Local and Actions-runner working state lives under `.state/` (for example
  `.state/forecast_history.sqlite`, `.state/pipeline_health.json`, and
  `.state/x_post_registry.json`). `.state/` is gitignored. An Actions runner is
  ephemeral: its SQLite file is scratch during that run and is restored from
  the Release before production work. The runner cache of
  `.state/previous_forecast.json` is a scheduling convenience, not canonical
  history.
- Successful forecast predictions are inserted into SQLite as immutable
  first-write history. Outcomes are populated only after their exact target
  timestamp has matured. Local CLI runs can use a chosen local database; they
  do not publish to GitHub Releases unless an operator explicitly invokes the
  publication workflow/action. See [history schema](HISTORY_SCHEMA.md).
- Production's durable canonical forecast database and analysis export are
  `forecast_history.sqlite.gz` and `forecast_history.csv.gz` in the public GitHub
  Release `forecast-history-v1`; bounded rollback generations and X/health state
  are also Release assets. These assets are publicly accessible: do not treat
  forecast history as confidential or store secrets/private data in it. The
  Release is distinct from the optional independently administered private S3
  copy. Retention, integrity checks, failure order, and manual restore steps are
  in [history backup](HISTORY_BACKUP.md).
- Scheduled X publication follows forecast/data/drift/history health gates and
  an authenticated session preflight. A durable idempotency reservation is
  written before the X write; ambiguous outcomes are not automatically replayed.
  Manual publication is opt-in with `post_to_x=true`. See the
  [X publication runbook](X_POSTING.md).
- GitHub Pages is a generated **static site**: docs and forecast pages are built
  from the downloaded Release snapshot and uploaded as a Pages artifact. It is
  not a live API and does not serve SQLite queries. The read-only WSGI API is
  separate application code; there is no managed API service, systemd unit, or
  deployment provisioned by this repository. A live API deployment is
  operator-managed and must follow the [API service runbook](FORECAST_API_SERVICE.md)
  and [API contract](FORECAST_API_CONTRACT.md).

## Independent backup setup status

The independent S3-compatible backup implementation is merged, but configuration
is an outstanding production prerequisite tracked by [issue #355](https://github.com/jpfelgueiras/btc-timesfm/issues/355).
Repository metadata was inspected by variable/secret **names only**: no Actions
variables are configured, and the listed secrets are X credentials only. In
particular, these required names are currently absent:

- variable `HISTORY_INDEPENDENT_BACKUP_URI`;
- variable `HISTORY_BACKUP_AWS_REGION`;
- secrets `HISTORY_BACKUP_AWS_ACCESS_KEY_ID` and
  `HISTORY_BACKUP_AWS_SECRET_ACCESS_KEY`.

Until the operator provisions these values, the monitor and weekly drill fail
their explicit configuration check, while the production forecast fails during
history publication and opens an issue. On an existing Release, it may already
have uploaded and verified a Release rollback generation, but it does not replace
the canonical Release assets or prune generations after that failure. A first
publish likewise does not create the canonical Release. Do not treat the Release
copy alone as satisfying the independent-copy recovery objective. Configure and
verify the destination, credentials, region, access, and bucket retention using
the setup in [history backup](HISTORY_BACKUP.md) before relying on the S3 copy.
Recovery and incident response procedures are in
[disaster recovery](DISASTER_RECOVERY.md); do not duplicate those procedures
here.

## Failure triage

Start with the failing step and its run artifact. Preserve/download artifacts
before their retention expires; never put secret values in issues or logs.

| Symptom / step | First checks and operational effect | Runbook |
| --- | --- | --- |
| Source validation fails (`Validate market data and forecast BTC`, `market_data_validation.json`) | Inspect `market_data_validation.json`, `market_data_source.json`, and `source_health.json` for selected provider, validation codes, freshness, and fallback/quarantine status. Forecast may stop before model execution; unhealthy current data blocks X even if a forecast was generated. | [Market data](MARKET_DATA.md), [pipeline health](PIPELINE_HEALTH.md) |
| TimesFM load/inference or model contract error | Check forecast log around `model_load` / `model_inference`, observability stage/error, and whether model-cache/download initialization completed. A failed model step prevents a new forecast; no X post follows. Retry only after identifying transient download/runtime causes. | [Forecast lifecycle](FORECAST_WORKS.md), [observability](OBSERVABILITY.md) |
| History restore/schema/integrity or publication failure | Check `.state` restore/verify steps, schema and backup verification reports, Release permissions/assets, and S3 configuration. Unsupported/newer schemas are not silently rewritten. A failed canonical publish leaves existing canonical assets untouched; history health records failure, X publication is blocked, and the workflow opens a history-publication issue after a forecast succeeds. | [History schema](HISTORY_SCHEMA.md), [history backup](HISTORY_BACKUP.md), [disaster recovery](DISASTER_RECOVERY.md) |
| Pages build/deploy fails or site appears stale | Check Pages run's `Build documentation portal`, Release download, static-site generation/contract validation, and deploy job. A push/schedule/forecast completion triggers a rebuild, but it still depends on a readable canonical Release. This affects the static site, not a WSGI API. | This page; [forecast API service](FORECAST_API_SERVICE.md) |
| API `/readyz` is 503 or forecast endpoint returns `history_unavailable` | The repository does not deploy the API. In the operator-managed WSGI environment, check process liveness, `BTC_FORECAST_HISTORY_DB`, readable canonical SQLite and `BTC_PIPELINE_HEALTH_STATE`; readiness also fails when health/history is absent, stale, degraded, or unavailable. Use `/v1/health` and service logs to distinguish state from process failure. | [API service](FORECAST_API_SERVICE.md), [API contract](FORECAST_API_CONTRACT.md) |
| X preflight/post failure or duplicate lock | Review `x_post_status.json`, health summary, and durable registry state. Authentication preflight happens before reservation; ambiguous publish state requires checking X manually and must not be blindly retried. | [X publication](X_POSTING.md), [pipeline health](PIPELINE_HEALTH.md) |
| Independent monitor/drill failure | Check its JSON report and deduplicated incident issue; configuration absence is an expected explicit failure until issue #355 setup is complete. For recovery, use the documented scratch-only drill and restore procedure. | [Disaster recovery](DISASTER_RECOVERY.md), [history backup](HISTORY_BACKUP.md) |

Other scheduled research/maintenance workflows are visible in `.github/workflows/`;
their logs/artifacts are the source of truth for current details. For CI contracts
and required checks see [CI workflows](CI_WORKFLOWS.md). For broader backup,
recovery, API, and X incident procedures, follow the specialized runbooks linked
above rather than using this page as a replacement.
