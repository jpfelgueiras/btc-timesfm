# Forecast API service

`btc_timesfm.api.forecast_service` is a standard-library WSGI application for the `/v1` contract. It reads only the canonical `forecast_history.sqlite` database; it never writes, reconstructs, or substitutes forecast data.

## Configuration

Set `BTC_FORECAST_API_KEYS` to a comma-separated list of bearer tokens before creating the application. It refuses to start without a key. Optional configuration is `BTC_FORECAST_HISTORY_DB`, `BTC_PIPELINE_HEALTH_STATE`, `BTC_FORECAST_API_AUDIT_LOG`, `BTC_FORECAST_API_RATE_LIMIT` (default `60`), `BTC_FORECAST_API_RATE_WINDOW_SECONDS` (default `60`), `BTC_FORECAST_API_STALE_AFTER_SECONDS` (default `10800`), `BTC_FORECAST_API_AUDIT_MAX_BYTES` (default `10000000`), `BTC_FORECAST_API_AUDIT_BACKUPS` (default `3`), and `BTC_FORECAST_API_AUDIT_MAX_AGE_DAYS` (default `30`).

### Supported production topology

Run exactly one WSGI worker process (threads within that worker are supported) behind a reverse proxy/load balancer. The rate limiter, counters, and latency sample are process-local: adding workers or replicas multiplies the effective per-key rate limit and splits metrics. Multi-worker/multi-replica deployments are unsupported until the limiter and metrics use a shared backend; the hardening path is a shared atomic rate-limit store and a Prometheus-compatible aggregation/exporter. Do not use sticky sessions as a substitute. A dedicated proxy address must be configured in the WSGI server so `REMOTE_ADDR` is the original client address; never trust client-supplied forwarding headers. The proxy must terminate TLS, enforce HTTPS, and prevent direct public access to the WSGI listener. The service uses read-only SQLite history and requires the health JSON and audit directory to be on persistent, writable local storage; history must be readable by the worker. Do not share SQLite WAL state across hosts.

Set keys and paths before startup. Start the WSGI server under a supervisor with one worker and graceful termination enabled; allow in-flight requests to finish during shutdown. Do not expose the service directly through `wsgiref` outside local development.

## Security and operations

Every endpoint requires `Authorization: Bearer <token>` and the contract media type (or `*/*`). Limits are applied per hashed authenticated credential with an in-memory sliding window; `429` includes `Retry-After`. Tokens are compared in constant time and are never written to the audit log.

Every API request is appended as JSONL to `BTC_FORECAST_API_AUDIT_LOG` with timestamp, client address, path, status, latency, and returned forecast IDs. The active file rotates at the configured byte threshold, keeps the configured number of backups, and removes files older than the configured age on writes. Retained audit data is therefore bounded to `(backups + active) * max_bytes` and the configured age. Audit write/rotation failures are fail-open: the API response proceeds and the event may be lost; operators should monitor filesystem capacity and permissions. `/metrics` exposes scrapeable Prometheus text for request totals, status counts, history-read failures, and rolling-sample p95 latency. These metrics aggregate requests only within the one supported worker; scrape that process directly. `ForecastService.metrics.snapshot()` provides the same in-process view.

`/livez` is unauthenticated process liveness and does not touch dependencies. `/readyz` is unauthenticated dependency/pipeline readiness and returns `200` only when health state and canonical history are available and healthy; degraded/stale/unavailable pipeline state returns `503`. The authenticated `/v1/health` reports pipeline health details and can remain queryable when forecast reads are unavailable. Configure the orchestrator to restart on liveness failure and remove the instance from service on readiness failure; scrape `/metrics` separately.

The service reads durable pipeline health on every request. An unavailable or open history stage returns `503 history_unavailable` for forecast reads. Health remains queryable with `200` and `data.status: unavailable` while its state is readable. A stale canonical observation produces `freshness.status: stale` and a degraded health status; it is still returned as a contract-valid snapshot. Missing or unreadable health state returns `503` rather than guessing availability.

## Historical query bounds and SQLite plan

Historical requests apply their filters and keyset predicate in SQLite, order by `(origin_at DESC, horizon_hours ASC, model_name ASC)`, and fetch at most `limit + 1` rows. The extra row indicates whether a next cursor exists; Python only materializes the requested page. Cursors bind the normalized request filters and identify the complete ordering key, which is checked against the filtered history before use.

Schema migration 6 adds `idx_predictions_api_order` on `forecast_predictions(origin_at DESC, horizon_hours ASC, model_name ASC)`. Verify the ordering access path against a migrated database with:

```sql
EXPLAIN QUERY PLAN
SELECT origin_at, horizon_hours, model_name
FROM forecast_predictions
ORDER BY origin_at DESC, horizon_hours ASC, model_name ASC
LIMIT 101;
```

The plan should report `SCAN forecast_predictions USING COVERING INDEX idx_predictions_api_order` (SQLite versions may vary in wording) and no temporary B-tree for the ordering. The API test `test_api_order_index_is_used_by_sqlite` asserts the index name appears in this plan; the multi-page test checks complete ordered traversal with ties across page boundaries.
