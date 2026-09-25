"""Authenticated stdlib WSGI service for the immutable forecast history."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs

from btc_timesfm.api.forecast_contract import (
    API_VERSION,
    HEALTH_PATH,
    HISTORICAL_FORECASTS_PATH,
    LATEST_FORECAST_PATH,
    MEDIA_TYPE,
    validate_error_response,
    validate_health_response,
    validate_historical_response,
    validate_latest_response,
)

DEFAULT_LIMIT = 50
MAX_LIMIT = 100
_FINGERPRINT_SECRET = os.getenv("BTC_FORECAST_FINGERPRINT_SECRET")
if not _FINGERPRINT_SECRET:
    # Rate-limit fingerprints only need to be stable for this process lifetime.
    # Never use a source-controlled fallback for a cryptographic secret.
    _FINGERPRINT_SECRET = secrets.token_hex(32)
_FINGERPRINT_SECRET_BYTES = _FINGERPRINT_SECRET.encode("utf-8")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include an offset")
    return parsed.astimezone(timezone.utc)


def _safe_json(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


@dataclass(frozen=True)
class ServiceConfig:
    history_path: Path
    api_keys: frozenset[str]
    health_path: Path = Path(".state/pipeline_health.json")
    audit_path: Path = Path(".state/forecast_api_reads.jsonl")
    rate_limit: int = 60
    rate_window_seconds: int = 60
    stale_after_seconds: int = 10_800

    @classmethod
    def from_env(cls) -> "ServiceConfig":
        keys = frozenset(
            key.strip() for key in os.getenv("BTC_FORECAST_API_KEYS", "").split(",") if key.strip()
        )
        return cls(
            history_path=Path(
                os.getenv("BTC_FORECAST_HISTORY_DB", ".state/forecast_history.sqlite")
            ),
            api_keys=keys,
            health_path=Path(os.getenv("BTC_PIPELINE_HEALTH_STATE", ".state/pipeline_health.json")),
            audit_path=Path(
                os.getenv("BTC_FORECAST_API_AUDIT_LOG", ".state/forecast_api_reads.jsonl")
            ),
            rate_limit=int(os.getenv("BTC_FORECAST_API_RATE_LIMIT", "60")),
            rate_window_seconds=int(os.getenv("BTC_FORECAST_API_RATE_WINDOW_SECONDS", "60")),
            stale_after_seconds=int(os.getenv("BTC_FORECAST_API_STALE_AFTER_SECONDS", "10800")),
        )


@dataclass
class ServiceMetrics:
    requests_total: int = 0
    responses_by_status: dict[int, int] = field(default_factory=dict)
    history_read_failures: int = 0
    latency_ms: list[float] = field(default_factory=list)

    def record(self, status: int, latency_ms: float) -> None:
        self.requests_total += 1
        self.responses_by_status[status] = self.responses_by_status.get(status, 0) + 1
        self.latency_ms.append(latency_ms)
        del self.latency_ms[:-1_000]

    def snapshot(self) -> dict[str, Any]:
        latency = sorted(self.latency_ms)
        percentile = latency[max(0, int(len(latency) * 0.95) - 1)] if latency else 0.0
        return {
            "requests_total": self.requests_total,
            "responses_by_status": dict(self.responses_by_status),
            "history_read_failures": self.history_read_failures,
            "latency_p95_ms": round(percentile, 3),
        }


class SlidingWindowLimiter:
    def __init__(
        self, limit: int, window_seconds: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if limit < 1 or window_seconds < 1:
            raise ValueError("rate-limit configuration must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self.clock = clock
        self.requests: dict[str, deque[float]] = {}
        self.lock = threading.Lock()

    def allow(self, identity: str) -> tuple[bool, int]:
        now = self.clock()
        with self.lock:
            bucket = self.requests.setdefault(identity, deque())
            while bucket and bucket[0] <= now - self.window_seconds:
                bucket.popleft()
            if len(bucket) >= self.limit:
                retry_after = max(1, int(self.window_seconds - (now - bucket[0])) + 1)
                return False, retry_after
            bucket.append(now)
        return True, 0


class ForecastService:
    def __init__(self, config: ServiceConfig, *, clock: Callable[[], datetime] = _now) -> None:
        if not config.api_keys:
            raise ValueError("BTC_FORECAST_API_KEYS must contain at least one API key")
        self.config = config
        self.clock = clock
        self.limiter = SlidingWindowLimiter(config.rate_limit, config.rate_window_seconds)
        self.metrics = ServiceMetrics()

    def __call__(
        self, environ: Mapping[str, Any], start_response: Callable[..., Any]
    ) -> list[bytes]:
        started = time.perf_counter()
        status, headers, payload, audit = self.handle(environ)
        elapsed = (time.perf_counter() - started) * 1000.0
        self.metrics.record(status, elapsed)
        self._audit({**audit, "status": status, "latency_ms": round(elapsed, 3)})
        headers = [("Content-Type", MEDIA_TYPE), ("Cache-Control", "no-store"), *headers]
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        start_response(
            f"{status} {self._status_text(status)}", [*headers, ("Content-Length", str(len(body)))]
        )
        return [body]

    def handle(
        self, environ: Mapping[str, Any]
    ) -> tuple[int, list[tuple[str, str]], dict[str, Any], dict[str, Any]]:
        method = str(environ.get("REQUEST_METHOD", "GET"))
        path = str(environ.get("PATH_INFO", ""))
        query = parse_qs(str(environ.get("QUERY_STRING", "")), keep_blank_values=True)
        client = str(environ.get("REMOTE_ADDR") or "unknown")
        audit = {"timestamp": _iso(self.clock()), "method": method, "path": path, "client": client}
        if method != "GET":
            return self._error(400, "invalid_request", "Only GET requests are supported.", audit)
        if not self._accepts(environ.get("HTTP_ACCEPT")):
            return self._error(406, "not_acceptable", "Requested media type is unsupported.", audit)
        key = self._api_key(environ.get("HTTP_AUTHORIZATION"))
        if key is None or not any(
            hmac.compare_digest(key, valid) for valid in self.config.api_keys
        ):
            return self._error(401, "invalid_request", "Authentication is required.", audit)
        allowed, retry_after = self.limiter.allow(self._key_fingerprint(key))
        if not allowed:
            return self._error(
                429,
                "rate_limited",
                "Rate limit exceeded.",
                audit,
                [("Retry-After", str(retry_after))],
            )
        try:
            if path == HEALTH_PATH:
                return self._health(audit)
            if path == LATEST_FORECAST_PATH:
                return self._latest(query, audit)
            if path == HISTORICAL_FORECASTS_PATH:
                return self._historical(query, audit)
            return self._error(400, "invalid_request", "Unknown API endpoint.", audit)
        except (OSError, sqlite3.Error):
            self.metrics.history_read_failures += 1
            return self._error(503, "history_unavailable", "Durable history is unavailable.", audit)
        except (TypeError, ValueError, KeyError):
            return self._error(400, "invalid_request", "Request parameters are invalid.", audit)
        except Exception:
            return self._error(500, "internal_error", "Request could not be completed.", audit)

    def _latest(
        self, query: Mapping[str, list[str]], audit: dict[str, Any]
    ) -> tuple[int, list[tuple[str, str]], dict[str, Any], dict[str, Any]]:
        if query:
            raise ValueError("latest has no query parameters")
        health = self._health_state()
        if health is None or health["status"] == "unavailable":
            return self._error(503, "history_unavailable", "Durable history is unavailable.", audit)
        with self._connection() as connection:
            row = connection.execute("SELECT MAX(origin_at) FROM forecast_origins").fetchone()
            if row is None or row[0] is None:
                return self._error(404, "forecast_not_found", "No forecast is available.", audit)
            records = self._query_rows(connection, "o.origin_at = ?", [row[0]])
        if not records:
            return self._error(404, "forecast_not_found", "No forecast is available.", audit)
        payload = {
            "api_version": API_VERSION,
            "data": records[0],
            "freshness": self._freshness(records[0]["origin_at"]),
        }
        self._validate(payload, validate_latest_response)
        audit["forecast_ids"] = [records[0]["forecast_id"]]
        return 200, [], payload, audit

    def _historical(
        self, query: Mapping[str, list[str]], audit: dict[str, Any]
    ) -> tuple[int, list[tuple[str, str]], dict[str, Any], dict[str, Any]]:
        health = self._health_state()
        if health is None or health["status"] == "unavailable":
            return self._error(503, "history_unavailable", "Durable history is unavailable.", audit)
        filters, limit, cursor, signature = self._filters(query)
        with self._connection() as connection:
            records = self._query_rows(connection, filters[0], filters[1])
        start = self._cursor_index(records, cursor, signature) if cursor else 0
        page = records[start : start + limit]
        next_cursor = self._cursor(page[-1], signature) if len(records) > start + limit else None
        observed = page[0]["origin_at"] if page else self._latest_origin(records)
        payload = {
            "api_version": API_VERSION,
            "data": page,
            "pagination": {"limit": limit, "next_cursor": next_cursor},
            "freshness": self._freshness(observed),
        }
        self._validate(payload, validate_historical_response)
        audit["forecast_ids"] = [item["forecast_id"] for item in page]
        return 200, [], payload, audit

    def _health(
        self, audit: dict[str, Any]
    ) -> tuple[int, list[tuple[str, str]], dict[str, Any], dict[str, Any]]:
        health = self._health_state()
        if health is None:
            return self._error(503, "history_unavailable", "Health state is unavailable.", audit)
        payload = {"api_version": API_VERSION, "data": health}
        self._validate(payload, validate_health_response)
        return 200, [], payload, audit

    def _health_state(self) -> dict[str, Any] | None:
        try:
            raw = json.loads(self.config.health_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict) or not isinstance(raw.get("stages"), dict):
            return None
        stages = {
            str(name): {
                "health": str(item.get("health", "unknown")),
                "circuit_state": str(item.get("circuit_state", "unknown")),
            }
            for name, item in raw["stages"].items()
            if isinstance(item, dict)
        }
        try:
            observed = self._history_origin()
        except (OSError, sqlite3.Error):
            observed = None
            history_unavailable = True
        else:
            history_unavailable = False
        freshness = self._freshness(observed)
        overall = str(raw.get("overall_health", ""))
        status = (
            "unavailable"
            if history_unavailable
            or overall == "open"
            or any(stage["circuit_state"] == "open" for stage in stages.values())
            else "degraded"
            if overall == "degraded"
            or freshness["status"] != "fresh"
            or any(stage["health"] != "healthy" for stage in stages.values())
            else "healthy"
        )
        return {
            "checked_at": _iso(self.clock()),
            "status": status,
            "freshness": freshness,
            "stages": stages,
        }

    def _connection(self) -> sqlite3.Connection:
        if not self.config.history_path.is_file():
            raise OSError("history missing")
        connection = sqlite3.connect(f"file:{self.config.history_path.resolve()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def _history_origin(self) -> str | None:
        with self._connection() as connection:
            row = connection.execute("SELECT MAX(origin_at) FROM forecast_origins").fetchone()
        return str(row[0]) if row and row[0] else None

    def _query_rows(
        self, connection: sqlite3.Connection, where: str, params: list[Any]
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            f"""SELECT o.origin_at, o.generated_at, o.source_name, o.pair, o.source_price_usd, o.experiment_run_id, o.configuration_id, o.experiment_manifest_json, p.model_name, p.horizon_hours, p.target_at, p.predicted_price_usd, p.predicted_change_pct, p.q10_usd, p.q50_usd, p.q90_usd, p.model_agreement FROM forecast_predictions p JOIN forecast_origins o USING(origin_at) WHERE {where} ORDER BY o.origin_at DESC, p.horizon_hours ASC, p.model_name ASC""",
            params,
        ).fetchall()
        return [self._resource(row) for row in rows]

    def _resource(self, row: sqlite3.Row) -> dict[str, Any]:
        origin = str(row["origin_at"])
        model = str(row["model_name"])
        horizon = int(row["horizon_hours"])
        manifest = _safe_json(row["experiment_manifest_json"])
        source = {
            "name": row["source_name"],
            "pair": row["pair"],
            "price_usd": float(row["source_price_usd"]),
        }
        evidence: dict[str, Any] = {"model_agreement": row["model_agreement"]}
        if row["model_agreement"] is None:
            evidence["reason"] = "model agreement was not recorded"
        return {
            "forecast_id": f"{origin}:{model}:{horizon}",
            "origin_at": origin,
            "target_at": str(row["target_at"]),
            "horizon_hours": horizon,
            "model": {"name": model, "configuration_id": row["configuration_id"]},
            "estimate": {
                "price_usd": float(row["predicted_price_usd"]),
                "change_pct": float(row["predicted_change_pct"]),
            },
            "interval": {
                "level": 0.8,
                "lower_usd": row["q10_usd"],
                "median_usd": row["q50_usd"],
                "upper_usd": row["q90_usd"],
            },
            "confidence": {
                "score": float(row["model_agreement"] or 0.0),
                "label": "unknown" if row["model_agreement"] is None else "model_agreement",
                "evidence": evidence,
            },
            "run": {"id": row["experiment_run_id"], "generated_at": row["generated_at"]},
            "lineage": {
                "history_key": {"origin_at": origin, "model_name": model, "horizon_hours": horizon},
                "source": source,
                "manifest": manifest,
            },
            "freshness": self._freshness(origin),
        }

    def _filters(
        self, query: Mapping[str, list[str]]
    ) -> tuple[tuple[str, list[Any]], int, dict[str, Any] | None, dict[str, str]]:
        allowed = {
            "origin_from",
            "origin_to",
            "horizon_hours",
            "model",
            "run_id",
            "limit",
            "cursor",
        }
        if set(query) - allowed or any(len(values) != 1 for values in query.values()):
            raise ValueError("invalid query")
        clauses: list[str] = []
        params: list[Any] = []
        for name, operator in (("origin_from", ">="), ("origin_to", "<=")):
            if name in query:
                value = query[name][0]
                _parse_timestamp(value)
                clauses.append(f"o.origin_at {operator} ?")
                params.append(value)
        if "horizon_hours" in query:
            horizon = int(query["horizon_hours"][0])
            if horizon < 1:
                raise ValueError("invalid horizon")
            clauses.append("p.horizon_hours = ?")
            params.append(horizon)
        for name, column in (("model", "p.model_name"), ("run_id", "o.experiment_run_id")):
            if name in query and not query[name][0]:
                raise ValueError("empty filter")
            if name in query:
                clauses.append(f"{column} = ?")
                params.append(query[name][0])
        limit = int(query.get("limit", [str(DEFAULT_LIMIT)])[0])
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError("invalid limit")
        signature = {
            key: value[0] for key, value in query.items() if key not in {"cursor", "limit"}
        }
        cursor = self._decode_cursor(query["cursor"][0], signature) if "cursor" in query else None
        return (" AND ".join(clauses) or "1 = 1", params), limit, cursor, signature

    def _cursor_index(
        self, records: list[dict[str, Any]], cursor: dict[str, Any], signature: dict[str, str]
    ) -> int:
        if cursor.get("filters") != signature:
            raise ValueError("cursor filters differ")
        key = cursor.get("key")
        for index, record in enumerate(records):
            if [record["origin_at"], record["horizon_hours"], record["model"]["name"]] == key:
                return index + 1
        raise ValueError("cursor not found")

    def _cursor(self, record: dict[str, Any], filters: dict[str, str]) -> str:
        raw = json.dumps(
            {
                "filters": filters,
                "key": [record["origin_at"], record["horizon_hours"], record["model"]["name"]],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def _decode_cursor(self, value: str, filters: dict[str, str]) -> dict[str, Any]:
        try:
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            parsed = json.loads(raw)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("invalid cursor") from None
        if (
            not isinstance(parsed, dict)
            or parsed.get("filters") != filters
            or not isinstance(parsed.get("key"), list)
        ):
            raise ValueError("invalid cursor")
        return parsed

    def _latest_origin(self, records: list[dict[str, Any]]) -> str | None:
        return records[0]["origin_at"] if records else self._history_origin()

    def _freshness(self, observed_at: str | None) -> dict[str, Any]:
        if observed_at is None:
            return {"observed_at": _iso(self.clock()), "age_seconds": 0.0, "status": "unknown"}
        age = max(0.0, (self.clock() - _parse_timestamp(observed_at)).total_seconds())
        return {
            "observed_at": observed_at,
            "age_seconds": round(age, 3),
            "status": "fresh" if age <= self.config.stale_after_seconds else "stale",
        }

    def _accepts(self, header: object) -> bool:
        return header in (None, "", "*/*") or MEDIA_TYPE in str(header)

    def _api_key(self, header: object) -> str | None:
        if not isinstance(header, str) or not header.startswith("Bearer "):
            return None
        value = header.removeprefix("Bearer ").strip()
        return value or None

    def _key_fingerprint(self, key: str) -> str:
        return hashlib.blake2b(
            key.encode("utf-8"), key=_FINGERPRINT_SECRET_BYTES, digest_size=8
        ).hexdigest()

    def _error(
        self,
        status: int,
        code: str,
        message: str,
        audit: dict[str, Any],
        headers: list[tuple[str, str]] | None = None,
    ) -> tuple[int, list[tuple[str, str]], dict[str, Any], dict[str, Any]]:
        payload = {"api_version": API_VERSION, "error": {"code": code, "message": message}}
        self._validate(payload, validate_error_response)
        return status, headers or [], payload, audit

    def _validate(self, payload: dict[str, Any], validator: Callable[[Any], list[str]]) -> None:
        if validator(payload):
            raise RuntimeError("server generated an invalid contract response")

    def _audit(self, event: dict[str, Any]) -> None:
        self.config.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.config.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")

    @staticmethod
    def _status_text(status: int) -> str:
        return {
            200: "OK",
            400: "Bad Request",
            401: "Unauthorized",
            404: "Not Found",
            406: "Not Acceptable",
            429: "Too Many Requests",
            500: "Internal Server Error",
            503: "Service Unavailable",
        }[status]


def create_app() -> ForecastService:
    return ForecastService(ServiceConfig.from_env())


__all__ = ["ForecastService", "ServiceConfig", "SlidingWindowLimiter", "create_app"]
