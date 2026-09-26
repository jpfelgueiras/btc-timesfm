# API

This page describes the HTTP behavior implemented by the repository's read-only
forecast WSGI application. The repository supplies the application and its
contract; it does **not** deploy a production WSGI server or publish a live API
endpoint. There is no API hostname to call from these examples. The Pages
forecast dashboard is a static site, not an API endpoint.

For implementation-level details, see the [versioned contract](FORECAST_API_CONTRACT.md)
and [service guide](FORECAST_API_SERVICE.md). The corresponding contract and
service behavior are exercised in `tests/api/test_forecast_contract.py` and
`tests/api/test_forecast_service.py`.

## Base behavior

All application API routes accept `GET` only. API calls use a bearer API key
and the versioned media type:

```http
Authorization: Bearer <API_KEY>
Accept: application/vnd.btc-timesfm.forecast.v1+json
```

The `Accept` header may also be omitted or set to `*/*`. Successful and error
API bodies are JSON with `Content-Type:
application/vnd.btc-timesfm.forecast.v1+json`, `Cache-Control: no-store`, and
an `api_version` of `v1`. Do not copy a real credential into documentation or
logs. The app does not support writes.

## `GET /v1/forecasts/latest`

Returns the forecast associated with the greatest available forecast origin.
This route takes no query parameters. Its successful body is:

```json
{
  "api_version": "v1",
  "data": {
    "forecast_id": "<origin_at>:<model-name>:<horizon-hours>",
    "origin_at": "<RFC-3339 timestamp>",
    "target_at": "<RFC-3339 timestamp>",
    "horizon_hours": 4,
    "model": {"name": "<model>", "configuration_id": "<id-or-null>"},
    "estimate": {"price_usd": 62500.0, "change_pct": 1.2},
    "interval": {
      "level": 0.8,
      "lower_usd": 61000.0,
      "median_usd": 62400.0,
      "upper_usd": 64000.0
    },
    "confidence": {
      "score": 0.74,
      "label": "model_agreement",
      "evidence": {"model_agreement": 0.74}
    },
    "run": {"id": "<run-id-or-null>", "generated_at": "<RFC-3339 timestamp-or-null>"},
    "lineage": {
      "history_key": {
        "origin_at": "<RFC-3339 timestamp>",
        "model_name": "<model>",
        "horizon_hours": 4
      },
      "source": {"name": "<source>", "pair": "BTC/USD", "price_usd": 61760.0},
      "manifest": {"<recorded manifest fields>": "<value>"}
    },
    "freshness": {
      "observed_at": "<RFC-3339 timestamp>",
      "age_seconds": 180.0,
      "status": "fresh"
    }
  },
  "freshness": {
    "observed_at": "<RFC-3339 timestamp>",
    "age_seconds": 180.0,
    "status": "fresh"
  }
}
```

The values above are illustrative schema values, not a live response. Timestamps
are timezone-aware RFC 3339 strings. Prices and percentages are finite numbers;
interval bounds may be `null` and, when all are numeric, are ordered lower,
median, upper. Interval `level` is between zero and one. `forecast_id`
serializes the origin, model name, and horizon. The `confidence` object
describes model agreement evidence; it is **not** a probability or general
measure of forecast confidence. If agreement was not recorded, the service
uses a score of `0.0`, label `unknown`, and evidence explaining its absence.

## `GET /v1/forecasts`

Returns a page of historical forecast resources in stable order: origin newest
first, then horizon ascending, then model name ascending. Optional query
parameters are:

| Parameter | Meaning |
| --- | --- |
| `origin_from` | Inclusive lower bound for `origin_at`; timezone-aware timestamp. |
| `origin_to` | Inclusive upper bound for `origin_at`; timezone-aware timestamp. |
| `horizon_hours` | Positive integer horizon. |
| `model` | Exact model-name filter. |
| `run_id` | Exact experiment run ID filter. |
| `limit` | Page size from 1 through 100; defaults to 50. |
| `cursor` | Opaque continuation token returned as `pagination.next_cursor`. |

Each parameter may occur only once; unknown parameters are invalid. A cursor
continues the keyset traversal and is bound to the filters used on its page. Do
not construct or edit cursor values. `next_cursor` is `null` when there is no
next page. A successful response has this shape:

```json
{
  "api_version": "v1",
  "data": [],
  "pagination": {"limit": 50, "next_cursor": "<opaque-token-or-null>"},
  "freshness": {
    "observed_at": "<RFC-3339 timestamp>",
    "age_seconds": 180.0,
    "status": "fresh"
  }
}
```

Each item in `data`, when present, uses the forecast resource schema shown
above for `latest.data`. `data` may be empty. The page-level freshness
describes the newest history origin observed for the query/history snapshot;
each forecast also carries its own freshness object.

## `GET /v1/health`

Returns application health data when its health state can be read and
validated. A valid response is HTTP 200, including when the reported state is
degraded or unavailable:

```json
{
  "api_version": "v1",
  "data": {
    "checked_at": "<RFC-3339 timestamp>",
    "status": "healthy",
    "freshness": {
      "observed_at": "<RFC-3339 timestamp>",
      "age_seconds": 180.0,
      "status": "fresh"
    },
    "stages": {
      "history": {"health": "healthy", "circuit_state": "closed"}
    }
  }
}
```

Health `status` is `healthy`, `degraded`, or `unavailable`; freshness status is
`fresh`, `stale`, or `unknown`. The stages object reports stage health and
circuit state. Operational readiness uses a stricter rule described below.

## Errors and status codes

Error responses use the same versioned media type and this envelope:

```json
{
  "api_version": "v1",
  "error": {"code": "<stable-code>", "message": "<safe explanation>"}
}
```

| Status | Error code | Meaning |
| --- | --- | --- |
| `400` | `invalid_request` | Unsupported method/path, invalid query, timestamp, or cursor. Only `GET` is supported. |
| `401` | `invalid_request` | Missing or invalid bearer key. |
| `404` | `forecast_not_found` | No forecast exists for the latest route. |
| `406` | `not_acceptable` | `Accept` does not allow the v1 media type. |
| `429` | `rate_limited` | Request limit exceeded; includes `Retry-After` seconds. |
| `500` | `internal_error` | Unexpected request failure. |
| `503` | `history_unavailable` | Durable history or health state cannot be read, or forecast history is unavailable. |

The error message is safe to return but should not be treated as a machine
interface; use the stable code and HTTP status. Authentication is checked
after method and media-type validation, and before the rate limit and route
handling.

## Operational routes

These routes are implemented by the WSGI app and are intended for local
probes/scraping. They are not versioned JSON API resources and bypass API-key
authentication:

* `GET /livez` returns `200`, `text/plain; charset=utf-8`, body `ok\n` when
  the process can answer.
* `GET /readyz` returns `200` with `ready\n` only when health state is healthy;
  otherwise it returns `503` with `not ready\n`. The health check requires
  readable, valid pipeline-health state and usable history, and considers stale
  history or degraded stages not ready.
* `GET /metrics` returns Prometheus text exposition (`text/plain; version=0.0.4;
  charset=utf-8`) with request totals, response totals by status, history-read
  failures, and observed request-latency p95. These in-memory metrics and
  counters are process-local.

## Configuration and deployment boundary

`create_app()` constructs the WSGI application from environment configuration.
At least one key must be supplied via `BTC_FORECAST_API_KEYS` (comma-separated
keys); startup fails without one. Other service settings are:

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `BTC_FORECAST_HISTORY_DB` | `.state/forecast_history.sqlite` | Read-only durable forecast history. |
| `BTC_PIPELINE_HEALTH_STATE` | `.state/pipeline_health.json` | Pipeline health state used by health/readiness. |
| `BTC_FORECAST_API_RATE_LIMIT` | `60` | Requests per key per sliding window. |
| `BTC_FORECAST_API_RATE_WINDOW_SECONDS` | `60` | Sliding-window duration. |
| `BTC_FORECAST_API_STALE_AFTER_SECONDS` | `10800` | Age threshold for fresh/stale classification. |
| `BTC_FORECAST_API_AUDIT_LOG` | `.state/forecast_api_reads.jsonl` | Read-audit JSONL path. |
| `BTC_FORECAST_API_AUDIT_MAX_BYTES` | `10000000` | Maximum bytes per audit file before rotation. |
| `BTC_FORECAST_API_AUDIT_BACKUPS` | `3` | Number of rotated audit files retained. |
| `BTC_FORECAST_API_AUDIT_MAX_AGE_DAYS` | `30` | Audit-file age retention limit. |
| `BTC_FORECAST_FINGERPRINT_SECRET` | generated per process | Optional secret stabilizing API-key rate-limit fingerprints during process lifetime. |

The limiter and metrics are in memory, so the supported topology is a
**single WSGI worker** for coherent per-key limits and scrape counters. Cursor
tokens encode their continuation key and filters. Running multiple workers
gives each worker separate limiter and metric state. Audit events record timestamp,
method, path, remote address, status, latency, and returned forecast IDs, not
the bearer key; audit persistence is best-effort and does not fail an otherwise
completed read. Audit files rotate under the configured size/backup/age bounds.

Deployers must provide a production WSGI server, network/TLS termination,
secret provisioning, filesystem access to the history and health files, and
the single-worker topology themselves. This repository's WSGI app/contract
does not imply a deployed service or public live endpoint. See the [service
guide](FORECAST_API_SERVICE.md) for the application runtime and operational
details.
