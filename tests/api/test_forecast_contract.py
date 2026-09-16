"""Tests for the versioned read-only forecast API contract."""

from __future__ import annotations

import copy
import unittest

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


def forecast() -> dict:
    return {
        "forecast_id": "2026-09-16T12:00:00Z:ensemble:4",
        "origin_at": "2026-09-16T12:00:00Z",
        "target_at": "2026-09-16T16:00:00Z",
        "horizon_hours": 4,
        "model": {"name": "ensemble", "configuration_id": "sha256:configuration"},
        "estimate": {"price_usd": 62500.0, "change_pct": 1.2},
        "interval": {
            "level": 0.8,
            "lower_usd": 61000.0,
            "median_usd": 62400.0,
            "upper_usd": 64000.0,
        },
        "confidence": {"score": 0.74, "label": "moderate", "evidence": {"model_agreement": 0.81}},
        "run": {"id": "run-123", "generated_at": "2026-09-16T12:03:00Z"},
        "lineage": {
            "history_key": {
                "origin_at": "2026-09-16T12:00:00Z",
                "model_name": "ensemble",
                "horizon_hours": 4,
            },
            "source": {"name": "kraken", "pair": "BTC/USD", "price_usd": 61760.0},
            "manifest": {"git_sha": "abc", "input_sha256": "def"},
        },
        "freshness": {
            "observed_at": "2026-09-16T12:00:00Z",
            "age_seconds": 180.0,
            "status": "fresh",
        },
    }


def freshness() -> dict:
    return {"observed_at": "2026-09-16T12:00:00Z", "age_seconds": 180.0, "status": "fresh"}


class TestForecastContract(unittest.TestCase):
    def test_contract_constants_are_versioned(self) -> None:
        self.assertEqual(API_VERSION, "v1")
        self.assertEqual(LATEST_FORECAST_PATH, "/v1/forecasts/latest")
        self.assertEqual(HISTORICAL_FORECASTS_PATH, "/v1/forecasts")
        self.assertEqual(HEALTH_PATH, "/v1/health")
        self.assertEqual(MEDIA_TYPE, "application/vnd.btc-timesfm.forecast.v1+json")

    def test_latest_response_is_valid(self) -> None:
        payload = {"api_version": API_VERSION, "data": forecast(), "freshness": freshness()}
        self.assertEqual(validate_latest_response(payload), [])

    def test_historical_response_is_valid_and_paginates(self) -> None:
        payload = {
            "api_version": API_VERSION,
            "data": [forecast()],
            "pagination": {"limit": 50, "next_cursor": "opaque-cursor"},
            "freshness": freshness(),
        }
        self.assertEqual(validate_historical_response(payload), [])

    def test_health_response_is_valid(self) -> None:
        payload = {
            "api_version": API_VERSION,
            "data": {
                "checked_at": "2026-09-16T12:03:00Z",
                "status": "degraded",
                "freshness": freshness(),
                "stages": {"history": {"health": "degraded", "circuit_state": "closed"}},
            },
        }
        self.assertEqual(validate_health_response(payload), [])

    def test_error_response_is_valid(self) -> None:
        payload = {
            "api_version": API_VERSION,
            "error": {"code": "history_unavailable", "message": "Durable history is unavailable."},
        }
        self.assertEqual(validate_error_response(payload), [])

    def test_unknown_error_code_is_rejected(self) -> None:
        payload = {
            "api_version": API_VERSION,
            "error": {"code": "database_exception", "message": "Request failed."},
        }
        self.assertTrue(any("error.code" in error for error in validate_error_response(payload)))

    def test_missing_traceability_field_is_rejected(self) -> None:
        item = forecast()
        del item["lineage"]
        payload = {"api_version": API_VERSION, "data": item, "freshness": freshness()}
        self.assertIn("forecast.lineage is required", validate_latest_response(payload))

    def test_negative_freshness_age_is_rejected(self) -> None:
        item = forecast()
        item["freshness"] = {
            "observed_at": "2026-09-16T12:00:00Z",
            "age_seconds": -1,
            "status": "fresh",
        }
        payload = {"api_version": API_VERSION, "data": item, "freshness": freshness()}
        self.assertTrue(any("age_seconds" in error for error in validate_latest_response(payload)))

    def test_forecast_identity_and_interval_order_are_rejected_when_invalid(self) -> None:
        item = forecast()
        item["forecast_id"] = "not-the-history-key"
        item["interval"]["lower_usd"] = 65000.0
        payload = {"api_version": API_VERSION, "data": item, "freshness": freshness()}
        errors = validate_latest_response(payload)
        self.assertTrue(any("forecast_id" in error for error in errors))
        self.assertTrue(any("bounds" in error for error in errors))

    def test_invalid_cursor_pagination_shape_is_rejected(self) -> None:
        payload = {
            "api_version": API_VERSION,
            "data": [forecast()],
            "pagination": {"limit": 101, "next_cursor": 1},
            "freshness": freshness(),
        }
        errors = validate_historical_response(payload)
        self.assertTrue(any("limit" in error for error in errors))
        self.assertTrue(any("next_cursor" in error for error in errors))

    def test_response_version_is_rejected_when_incompatible(self) -> None:
        payload = {"api_version": "v2", "data": forecast(), "freshness": freshness()}
        self.assertTrue(any("api_version" in error for error in validate_latest_response(payload)))

    def test_historical_items_are_validated_individually(self) -> None:
        item = copy.deepcopy(forecast())
        item["horizon_hours"] = 0
        payload = {
            "api_version": API_VERSION,
            "data": [item],
            "pagination": {"limit": 1, "next_cursor": None},
            "freshness": freshness(),
        }
        self.assertIn(
            "response.data[0].horizon_hours must be a positive integer",
            validate_historical_response(payload),
        )


if __name__ == "__main__":
    unittest.main()
