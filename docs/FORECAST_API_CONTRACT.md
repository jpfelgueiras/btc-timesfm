# Read-only forecast API contract v1

This is the implementation-independent contract for the public read-only forecast API. It defines data that issue #219 may serve; it does not create an HTTP server.

## Versioning and availability

All endpoints are rooted at `/v1` and return `Content-Type: application/vnd.btc-timesfm.forecast.v1+json`. Success and error bodies contain `api_version: "v1"`.

`v1` is additive-only: existing fields retain their meaning and type; new optional fields may be added. Removing, renaming, changing a field type, changing cursor semantics, or changing error codes requires a new path version. Clients must ignore unknown fields. The server returns `406 not_acceptable` when the requested media type/version is unsupported.

Responses are read-only snapshots. `200` means the representation was read successfully, not that it is fresh. Inspect `freshness.status` and `GET /v1/health` before use. `503` means the requested representation cannot safely be read from the durable history or current health state.

## Endpoints

| Method and path | Purpose | Success |
| --- | --- | --- |
| `GET /v1/forecasts/latest` | Latest immutable origin and its forecasts. | `200` latest response |
| `GET /v1/forecasts` | Historical immutable forecast rows. | `200` historical response |
| `GET /v1/health` | API availability, pipeline health, and source freshness. | `200` health response |

### Latest

`GET /v1/forecasts/latest` returns the newest `origin_at` in durable history. If history has no forecast yet, return `404 forecast_not_found`, not an empty forecast object.

### Historical filters and pagination

`GET /v1/forecasts` accepts optional `origin_from` and `origin_to` RFC 3339 timestamps, `horizon_hours` positive integer, `model` exact model name, `run_id` exact experiment run ID, `limit` (default 50, range 1–100), and opaque `cursor`.

Rows are ordered by `origin_at` descending, then `horizon_hours` ascending, then `model.name` ascending. The cursor identifies the last returned logical row and is valid only with the identical filters and API version. Do not construct or parse it as a client. The response returns `pagination.next_cursor: null` at the end. A valid filter combination with no rows returns `200` and `data: []`; malformed filters or an invalid/mismatched cursor return `400 invalid_request`.

## Forecast resource

Every item, including `latest.data`, has these required fields:

```json
{
  "forecast_id": "2026-09-16T12:00:00Z:ensemble:4",
  "origin_at": "2026-09-16T12:00:00Z",
  "target_at": "2026-09-16T16:00:00Z",
  "horizon_hours": 4,
  "model": {"name": "ensemble", "configuration_id": "sha256:..."},
  "estimate": {"price_usd": 62500.0, "change_pct": 1.2},
  "interval": {"level": 0.8, "lower_usd": 61000.0, "median_usd": 62400.0, "upper_usd": 64000.0},
  "confidence": {"score": 0.74, "label": "moderate", "evidence": {"model_agreement": 0.81}},
  "run": {"id": "run-...", "generated_at": "2026-09-16T12:03:00Z"},
  "lineage": {"history_key": {"origin_at": "2026-09-16T12:00:00Z", "model_name": "ensemble", "horizon_hours": 4}, "source": {"name": "kraken", "pair": "BTC/USD", "price_usd": 61760.0}, "manifest": {"git_sha": "...", "input_sha256": "..."}},
  "freshness": {"observed_at": "2026-09-16T12:00:00Z", "age_seconds": 180.0, "status": "fresh"}
}
```

`forecast_id` is a stable serialization of the immutable logical key `(origin_at, model.name, horizon_hours)`. `origin_at` is the source candle close available to the forecast; `target_at` is its horizon target. All timestamps are RFC 3339 timestamps with an offset. Prices are finite USD numbers and `change_pct` is relative to `lineage.source.price_usd`. `interval.level` is a finite nominal coverage level strictly between zero and one; when present, `lower_usd <= median_usd <= upper_usd`. Interval bounds and `median_usd` may be null only when the forecast run did not produce calibrated quantiles. `confidence.score` is a finite number from zero through one, and `confidence.evidence` preserves the evidence used for the confidence judgement rather than inferring it at read time.

`run.id` is the immutable `experiment_run_id`; `model.configuration_id` is the manifest configuration fingerprint. `lineage.history_key` maps exactly to the first-write-wins durable history row. `lineage.manifest` is the versioned experiment manifest, including code, configuration, provider/window, and input digest needed to reproduce the estimate. The server must not invent missing lineage: use `null` for unavailable optional values and retain a documented reason in `confidence.evidence` or `lineage`. `forecast_id` must exactly serialize the history key as `{origin_at}:{model.name}:{horizon_hours}`.

Each freshness object contains the relevant observation timestamp, non-negative age at response generation, and one of `fresh`, `stale`, or `unknown`. `latest` and historical envelopes also have a top-level `freshness` describing the collection/read state.

## Envelopes, health, and errors

Latest success:

```json
{"api_version":"v1","data":{},"freshness":{"observed_at":"2026-09-16T12:00:00Z","age_seconds":180.0,"status":"fresh"}}
```

Historical success additionally contains `pagination: {"limit": 50, "next_cursor": null}`.

Health success is:

```json
{"api_version":"v1","data":{"checked_at":"2026-09-16T12:03:00Z","status":"healthy","freshness":{"observed_at":"2026-09-16T12:00:00Z","age_seconds":180.0,"status":"fresh"},"stages":{"market_data":{"health":"healthy","circuit_state":"closed"},"forecast":{"health":"healthy","circuit_state":"closed"},"history":{"health":"healthy","circuit_state":"closed"}}}}
```

Health `status` is `healthy`, `degraded`, or `unavailable`. It is derived from the durable pipeline-health report; stage names and states preserve its audit trail. `GET /v1/health` remains the first availability check: return `200` for known healthy/degraded state and `503` only when health itself cannot be determined.

All non-success responses use `{"api_version":"v1","error":{"code":"...","message":"..."}}`. Codes are stable: `invalid_request` (400), `forecast_not_found` (404), `not_acceptable` (406), `rate_limited` (429), `history_unavailable` (503), and `internal_error` (500). Error messages are safe for clients and must not expose filesystem paths, credentials, or stack traces.

## Schema tests

`btc_timesfm.api.forecast_contract` exports endpoint paths, media type, and validators for latest, historical, health, and error response bodies. The tests define contract fixtures and rejection cases. Implementations must validate emitted payloads with these functions before exposing the API.
