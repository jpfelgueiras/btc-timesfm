# Pipeline health and circuit breakers

The scheduled forecast workflow keeps a small durable health state in `.state/pipeline_health.json`. The file is uploaded beside the forecast-history assets so consecutive failures survive GitHub Actions runners and cache eviction.

## Stage health

Four production stages have explicit health and circuit state:

| Stage | Persistent-failure threshold | Purpose |
| --- | ---: | --- |
| `market_data` | 2 | input fetch/validation health |
| `forecast` | 2 | forecast execution health |
| `history` | 2 | durable-history publication health |
| `x_post` | 3 | X session/publication health |

A first failure is **transient**: the stage becomes `degraded`, but its circuit remains closed. Consecutive failures reaching the threshold are **persistent**: the stage becomes `open` and its circuit opens. A successful observation deterministically resets the consecutive-failure count to zero and closes the circuit.

The state records last success/failure times, failure class/detail and a bounded event history. `pipeline_health_report.json` and `pipeline_health_summary.md` provide a per-run view.

## Publication safety gate

Generating and persisting a forecast is intentionally separate from publishing it publicly. The X post is blocked when any of these conditions is present:

- the current market-data validation is unhealthy;
- the current forecast step failed;
- model/feature drift is `severe`;
- durable-history publication failed in the current run;
- the market-data, forecast or history circuit is open;
- the X-post circuit is open and not ready for a recovery probe.

This means a severe drift state can still be recorded in forecast history while public publication is suppressed. Skipping a public post is preferred to publishing from degraded evidence.

The workflow uses two gates. A preliminary gate checks data/forecast/drift/X health before the X session preflight and idempotency reservation. The durable history and reservation are then persisted. A final gate verifies current history health immediately before the X write.

## X circuit recovery

Three consecutive X failures open the X circuit. The default cooldown is 120 minutes and can be overridden with `BTC_X_CIRCUIT_COOLDOWN_MINUTES`.

After the cooldown, exactly one gate evaluation changes the circuit to `half_open` and allows one recovery probe. A second concurrent/repeated gate sees `half_open` and blocks. A successful X preflight/publication closes the circuit and resets the counter. A failed probe reopens it and restarts the cooldown.

This behavior combines with the durable X idempotency registry: a health recovery attempt is never allowed to blindly replay an ambiguous prior publication.

## Current-run vs persistent failures

Circuit thresholds distinguish transient from persistent failures, but some current failures are immediately unsafe for publication. For example, the first invalid market-data validation or the first failed durable-history upload blocks that run even though the corresponding circuit has not yet opened.

A later successful run resets that stage deterministically. No manual counter reset is required for normal recovery.

## Optional notification hook

If a repository secret named `PIPELINE_HEALTH_WEBHOOK_URL` is configured, the workflow can POST a small sanitized JSON health notification when health is degraded/open or publication is blocked. The webhook URL itself is never included in the payload or output.

If the secret is absent, notification is a no-op. The core health/circuit behavior does not depend on any external notification service.

## Operational files

- `.state/pipeline_health.json` — durable cross-run state
- `pipeline_health_report.json` — current machine-readable health report
- `pipeline_health_summary.md` — current Actions-friendly summary
- `market_data_validation.json` — current data-health signal
- `drift_report.json` — current drift signal
- `x_post_status.json` — X preflight/publication signal

All health-state transitions are deterministic from these observations plus the persisted prior state and configured cooldown/thresholds.
