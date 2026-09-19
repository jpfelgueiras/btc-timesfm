"""Tests for authenticated, audited forecast HTTP service behavior."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from btc_timesfm.api.forecast_contract import (
    validate_error_response,
    validate_health_response,
    validate_historical_response,
    validate_latest_response,
)
from btc_timesfm.api.forecast_service import ForecastService, ServiceConfig
from btc_timesfm.history.history_store import ForecastHistoryStore

NOW = datetime(2026, 9, 16, 12, 3, tzinfo=timezone.utc)
ORIGIN = "2026-09-16T12:00:00Z"


def snapshot() -> dict[str, Any]:
    return {
        "generated_at": "2026-09-16T12:03:00Z",
        "latest_close_at": ORIGIN,
        "latest_close_usd": 61760.0,
        "source": "kraken",
        "pair": "BTC/USD",
        "experiment_manifest": {"run_id": "run-123", "configuration_id": "sha256:config"},
        "predictions": {
            "4h": {
                "price_usd": 62500.0,
                "change_pct": 1.2,
                "q10_usd": 61000.0,
                "q50_usd": 62400.0,
                "q90_usd": 64000.0,
                "model_agreement": 0.74,
            }
        },
    }


class TestForecastService(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.database = root / "history.sqlite"
        ForecastHistoryStore(self.database).ingest_snapshot(snapshot())
        self.health = root / "health.json"
        self.health.write_text(
            json.dumps({"stages": {"history": {"health": "healthy", "circuit_state": "closed"}}}),
            encoding="utf-8",
        )
        self.audit = root / "audit.jsonl"
        self.service = ForecastService(
            ServiceConfig(
                self.database, frozenset({"secret"}), self.health, self.audit, rate_limit=2
            ),
            clock=lambda: NOW,
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def request(
        self,
        path: str,
        *,
        auth: str | None = "Bearer secret",
        query: str = "",
        accept: str | None = None,
    ) -> tuple[int, dict[str, str], dict[str, Any]]:
        return self._request_service(self.service, path, auth=auth, query=query, accept=accept)

    def _request_service(
        self,
        service: ForecastService,
        path: str,
        *,
        auth: str | None = "Bearer secret",
        query: str = "",
        accept: str | None = None,
        method: str = "GET",
    ) -> tuple[int, dict[str, str], dict[str, Any]]:
        captured: dict[str, Any] = {}
        headers = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "REMOTE_ADDR": "test-client",
            "wsgi.input": io.BytesIO(),
        }
        if auth is not None:
            headers["HTTP_AUTHORIZATION"] = auth
        if accept is not None:
            headers["HTTP_ACCEPT"] = accept
        body = b"".join(
            service(
                headers,
                lambda status, response_headers: captured.update(
                    status=status, headers=response_headers
                ),
            )
        )
        return int(captured["status"].split()[0]), dict(captured["headers"]), json.loads(body)

    def test_authentication_media_type_and_rate_limit_are_enforced(self) -> None:
        status, _, body = self.request("/v1/forecasts/latest", auth=None)
        self.assertEqual(status, 401)
        self.assertEqual(validate_error_response(body), [])
        status, _, body = self.request("/v1/forecasts/latest", accept="application/json")
        self.assertEqual(status, 406)
        self.assertEqual(body["error"]["code"], "not_acceptable")
        self.request("/v1/health")
        self.request("/v1/health")
        status, headers, body = self.request("/v1/health")
        self.assertEqual(status, 429)
        self.assertIn("Retry-After", headers)
        self.assertEqual(body["error"]["code"], "rate_limited")

    def test_serves_canonical_history_and_audits_reads(self) -> None:
        status, _, body = self.request("/v1/forecasts/latest")
        self.assertEqual(status, 200)
        self.assertEqual(validate_latest_response(body), [])
        self.assertEqual(
            body["data"]["lineage"]["history_key"]["origin_at"], "2026-09-16T12:00:00+00:00"
        )
        status, _, body = self.request("/v1/forecasts")
        self.assertEqual(status, 200)
        self.assertEqual(validate_historical_response(body), [])
        events = [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events[-1]["forecast_ids"], [body["data"][0]["forecast_id"]])
        self.assertNotIn("secret", self.audit.read_text(encoding="utf-8"))

    def test_historical_filters_pagination_and_data_consistency(self) -> None:
        status, _, body = self.request(
            "/v1/forecasts", query="limit=1&horizon_hours=4&model=ensemble"
        )
        self.assertEqual(status, 200)
        self.assertEqual(validate_historical_response(body), [])
        self.assertEqual(len(body["data"]), 1)
        forecast = body["data"][0]
        self.assertEqual(forecast["forecast_id"], "2026-09-16T12:00:00+00:00:ensemble:4")
        self.assertEqual(forecast["horizon_hours"], 4)
        self.assertEqual(forecast["model"]["name"], "ensemble")
        self.assertEqual(forecast["estimate"]["price_usd"], 62500.0)
        self.assertEqual(forecast["lineage"]["source"]["pair"], "BTC/USD")
        self.assertEqual(forecast["lineage"]["source"]["price_usd"], 61760.0)
        self.assertIsNone(body["pagination"]["next_cursor"])
        self.assertEqual(body["pagination"]["limit"], 1)

    def test_endpoint_error_contracts_are_stable(self) -> None:
        cases = [
            ("/v1/unknown", "", "GET", 400, "invalid_request"),
            ("/v1/forecasts/latest", "unexpected=1", "GET", 400, "invalid_request"),
            ("/v1/forecasts", "limit=101", "GET", 400, "invalid_request"),
            ("/v1/forecasts", "cursor=invalid", "GET", 400, "invalid_request"),
            ("/v1/forecasts", "invalid=param", "GET", 400, "invalid_request"),
            ("/v1/forecasts", "", "POST", 400, "invalid_request"),
        ]
        for path, query, method, expected_status, expected_code in cases:
            with self.subTest(path=path, query=query, method=method):
                service = ForecastService(
                    ServiceConfig(self.database, frozenset({"secret"}), self.health, self.audit),
                    clock=lambda: NOW,
                )
                status, _, body = self._request_service(service, path, query=query, method=method)
                self.assertEqual(status, expected_status)
                self.assertEqual(body["error"]["code"], expected_code)
                self.assertEqual(validate_error_response(body), [])

    def test_validation_errors_and_empty_history_are_contract_compliant(self) -> None:
        empty = Path(self.directory.name) / "empty.sqlite"
        ForecastHistoryStore(empty)
        service = ForecastService(
            ServiceConfig(empty, frozenset({"secret"}), self.health, self.audit), clock=lambda: NOW
        )
        status, _, body = self._request_service(service, "/v1/forecasts/latest")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "forecast_not_found")
        self.assertEqual(validate_error_response(body), [])

    def test_unavailable_and_stale_health_are_explicit(self) -> None:
        self.health.write_text(
            json.dumps({"stages": {"history": {"health": "open", "circuit_state": "open"}}}),
            encoding="utf-8",
        )
        status, _, body = self.request("/v1/forecasts/latest")
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "history_unavailable")
        status, _, body = self.request("/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["status"], "unavailable")
        self.assertEqual(validate_health_response(body), [])
        unavailable_service = ForecastService(
            ServiceConfig(
                Path(self.directory.name) / "missing.sqlite",
                frozenset({"secret"}),
                self.health,
                self.audit,
            ),
            clock=lambda: NOW,
        )
        status, _, body = self._request_service(unavailable_service, "/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["status"], "unavailable")
        self.health.write_text(
            json.dumps({"stages": {"history": {"health": "healthy", "circuit_state": "closed"}}}),
            encoding="utf-8",
        )
        stale_service = ForecastService(
            ServiceConfig(
                self.database, frozenset({"secret"}), self.health, self.audit, stale_after_seconds=1
            ),
            clock=lambda: NOW,
        )
        status, _, body = self._request_service(stale_service, "/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["status"], "degraded")
        self.assertEqual(body["data"]["freshness"]["status"], "stale")

    def test_unavailable_history_and_health_errors_are_contract_compliant(self) -> None:
        locked_db = Path(self.directory.name) / "locked.sqlite"
        locked_db.write_text("not-a-db", encoding="utf-8")
        service = ForecastService(
            ServiceConfig(locked_db, frozenset({"secret"}), self.health, self.audit),
            clock=lambda: NOW,
        )
        status, _, body = self._request_service(service, "/v1/forecasts/latest")
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "history_unavailable")
        self.assertEqual(validate_error_response(body), [])
        self.health.write_text("invalid json", encoding="utf-8")
        status, _, body = self.request("/v1/health")
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "history_unavailable")
        self.assertEqual(validate_error_response(body), [])
