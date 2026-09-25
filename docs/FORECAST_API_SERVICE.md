# Forecast API service

`btc_timesfm.api.forecast_service` is a standard-library WSGI application for the `/v1` contract. It reads only the canonical `forecast_history.sqlite` database; it never writes, reconstructs, or substitutes forecast data.

## Configuration

Set `BTC_FORECAST_API_KEYS` to a comma-separated list of bearer tokens before creating the application. It refuses to start without a key. Optional configuration is `BTC_FORECAST_HISTORY_DB`, `BTC_PIPELINE_HEALTH_STATE`, `BTC_FORECAST_API_AUDIT_LOG`, `BTC_FORECAST_API_RATE_LIMIT` (default `60`), `BTC_FORECAST_API_RATE_WINDOW_SECONDS` (default `60`), and `BTC_FORECAST_API_STALE_AFTER_SECONDS` (default `10800`).

Serve with a production WSGI host which terminates TLS and supplies a trusted client address. For example, an application server may import `create_app` and call it as its WSGI app. Do not expose the service directly through `wsgiref` outside local development.

## Security and operations

Every endpoint requires `Authorization: Bearer <token>` and the contract media type (or `*/*`). Limits are applied per hashed authenticated credential with an in-memory sliding window; `429` includes `Retry-After`. Tokens are compared in constant time and are never written to the audit log.

Every attempted read is appended as JSONL to `BTC_FORECAST_API_AUDIT_LOG` with timestamp, client address, path, status, latency, and returned forecast IDs. `ForecastService.metrics.snapshot()` exposes request count, status counts, history-read failures, and p95 latency for the process lifetime.

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
