"""Schema validation for the versioned read-only forecast API contract."""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any, Mapping, cast

API_VERSION = "v1"
MEDIA_TYPE = "application/vnd.btc-timesfm.forecast.v1+json"

LATEST_FORECAST_PATH = "/v1/forecasts/latest"
HISTORICAL_FORECASTS_PATH = "/v1/forecasts"
HEALTH_PATH = "/v1/health"

ERROR_CODES = frozenset(
    {
        "invalid_request",
        "forecast_not_found",
        "not_acceptable",
        "rate_limited",
        "history_unavailable",
        "internal_error",
    }
)
FORECAST_REQUIRED_KEYS = frozenset(
    {
        "forecast_id",
        "origin_at",
        "target_at",
        "horizon_hours",
        "model",
        "estimate",
        "interval",
        "confidence",
        "run",
        "lineage",
        "freshness",
    }
)
FRESHNESS_REQUIRED_KEYS = frozenset({"observed_at", "age_seconds", "status"})
HEALTH_REQUIRED_KEYS = frozenset({"checked_at", "status", "freshness", "stages"})
ERROR_REQUIRED_KEYS = frozenset({"error"})
ERROR_DETAIL_REQUIRED_KEYS = frozenset({"code", "message"})
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")


def _is_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _mapping(value: Any, path: str, errors: list[str]) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        errors.append(f"{path} must be an object")
        return None
    return value


def _required_keys(
    value: Mapping[str, Any], required: frozenset[str], path: str, errors: list[str]
) -> None:
    for key in sorted(required - value.keys()):
        errors.append(f"{path}.{key} is required")


def _string(value: Any, path: str, errors: list[str], *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value:
        errors.append(f"{path} must be a non-empty string" + (" or null" if nullable else ""))


def _number(value: Any, path: str, errors: list[str], *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not _is_number(value):
        errors.append(f"{path} must be a finite number" + (" or null" if nullable else ""))


def _validate_freshness(value: Any, path: str, errors: list[str]) -> None:
    freshness = _mapping(value, path, errors)
    if freshness is None:
        return
    _required_keys(freshness, FRESHNESS_REQUIRED_KEYS, path, errors)
    if "observed_at" in freshness and not _is_timestamp(freshness["observed_at"]):
        errors.append(f"{path}.observed_at must be an RFC 3339 timestamp")
    if "age_seconds" in freshness and (
        not _is_number(freshness["age_seconds"]) or freshness["age_seconds"] < 0
    ):
        errors.append(f"{path}.age_seconds must be a non-negative finite number")
    if freshness.get("status") not in {"fresh", "stale", "unknown"}:
        errors.append(f"{path}.status must be fresh, stale, or unknown")


def _validate_forecast_id(value: Any, forecast: Mapping[str, Any], errors: list[str]) -> None:
    _string(value, "forecast.forecast_id", errors)
    origin_at = forecast.get("origin_at")
    model = forecast.get("model")
    horizon_hours = forecast.get("horizon_hours")
    if (
        isinstance(value, str)
        and isinstance(origin_at, str)
        and isinstance(model, Mapping)
        and isinstance(model.get("name"), str)
        and _is_positive_int(horizon_hours)
        and value != f"{origin_at}:{model['name']}:{horizon_hours}"
    ):
        errors.append(
            "forecast.forecast_id must serialize origin_at, model.name, and horizon_hours"
        )


def validate_forecast(value: Any) -> list[str]:
    """Return contract violations for a forecast resource without serving it."""
    errors: list[str] = []
    forecast = _mapping(value, "forecast", errors)
    if forecast is None:
        return errors
    _required_keys(forecast, FORECAST_REQUIRED_KEYS, "forecast", errors)
    for key in ("origin_at", "target_at"):
        if key in forecast and not _is_timestamp(forecast[key]):
            errors.append(f"forecast.{key} must be an RFC 3339 timestamp")
    if "horizon_hours" in forecast and not _is_positive_int(forecast["horizon_hours"]):
        errors.append("forecast.horizon_hours must be a positive integer")

    model = _mapping(forecast.get("model"), "forecast.model", errors)
    if model is not None:
        _string(model.get("name"), "forecast.model.name", errors)
        _string(
            model.get("configuration_id"), "forecast.model.configuration_id", errors, nullable=True
        )
    estimate = _mapping(forecast.get("estimate"), "forecast.estimate", errors)
    if estimate is not None:
        _number(estimate.get("price_usd"), "forecast.estimate.price_usd", errors)
        _number(estimate.get("change_pct"), "forecast.estimate.change_pct", errors)
    interval = _mapping(forecast.get("interval"), "forecast.interval", errors)
    if interval is not None:
        _number(interval.get("level"), "forecast.interval.level", errors)
        if _is_number(interval.get("level")) and not 0 < interval["level"] < 1:
            errors.append("forecast.interval.level must be greater than 0 and less than 1")
        for key in ("lower_usd", "median_usd", "upper_usd"):
            _number(interval.get(key), f"forecast.interval.{key}", errors, nullable=True)
        lower_usd = interval.get("lower_usd")
        median_usd = interval.get("median_usd")
        upper_usd = interval.get("upper_usd")
        numeric_bounds = _is_number(lower_usd) and _is_number(median_usd) and _is_number(upper_usd)
        if numeric_bounds and not (
            cast(float, lower_usd) <= cast(float, median_usd) <= cast(float, upper_usd)
        ):
            errors.append(
                "forecast.interval bounds must be ordered lower_usd, median_usd, upper_usd"
            )
    confidence = _mapping(forecast.get("confidence"), "forecast.confidence", errors)
    if confidence is not None:
        _number(confidence.get("score"), "forecast.confidence.score", errors)
        if _is_number(confidence.get("score")) and not 0 <= confidence["score"] <= 1:
            errors.append("forecast.confidence.score must be from 0 through 1")
        _string(confidence.get("label"), "forecast.confidence.label", errors)
        if not isinstance(confidence.get("evidence"), Mapping):
            errors.append("forecast.confidence.evidence must be an object")
    run = _mapping(forecast.get("run"), "forecast.run", errors)
    if run is not None:
        _string(run.get("id"), "forecast.run.id", errors, nullable=True)
        if run.get("generated_at") is not None and not _is_timestamp(run["generated_at"]):
            errors.append("forecast.run.generated_at must be an RFC 3339 timestamp or null")
    lineage = _mapping(forecast.get("lineage"), "forecast.lineage", errors)
    if lineage is not None:
        history_key = _mapping(lineage.get("history_key"), "forecast.lineage.history_key", errors)
        if history_key is not None:
            if not _is_timestamp(history_key.get("origin_at")):
                errors.append(
                    "forecast.lineage.history_key.origin_at must be an RFC 3339 timestamp"
                )
            _string(
                history_key.get("model_name"), "forecast.lineage.history_key.model_name", errors
            )
            if not _is_positive_int(history_key.get("horizon_hours")):
                errors.append(
                    "forecast.lineage.history_key.horizon_hours must be a positive integer"
                )
        for key in ("source", "manifest"):
            if lineage.get(key) is not None and not isinstance(lineage[key], Mapping):
                errors.append(f"forecast.lineage.{key} must be an object or null")
    _validate_forecast_id(forecast.get("forecast_id"), forecast, errors)
    _validate_freshness(forecast.get("freshness"), "forecast.freshness", errors)
    return errors


def _validate_version(response: Mapping[str, Any], errors: list[str]) -> None:
    if response.get("api_version") != API_VERSION:
        errors.append(f"response.api_version must equal {API_VERSION!r}")


def validate_latest_response(value: Any) -> list[str]:
    """Validate the success body of ``GET /v1/forecasts/latest``."""
    errors: list[str] = []
    response = _mapping(value, "response", errors)
    if response is None:
        return errors
    _validate_version(response, errors)
    errors.extend(validate_forecast(response.get("data")))
    _validate_freshness(response.get("freshness"), "response.freshness", errors)
    return errors


def validate_historical_response(value: Any) -> list[str]:
    """Validate the success body of ``GET /v1/forecasts``."""
    errors: list[str] = []
    response = _mapping(value, "response", errors)
    if response is None:
        return errors
    _validate_version(response, errors)
    data = response.get("data")
    if not isinstance(data, list):
        errors.append("response.data must be an array")
    else:
        for index, forecast in enumerate(data):
            errors.extend(
                error.replace("forecast", f"response.data[{index}]", 1)
                for error in validate_forecast(forecast)
            )
    pagination = _mapping(response.get("pagination"), "response.pagination", errors)
    if pagination is not None:
        _required_keys(
            pagination, frozenset({"limit", "next_cursor"}), "response.pagination", errors
        )
        if "limit" in pagination and (
            not isinstance(pagination["limit"], int)
            or isinstance(pagination["limit"], bool)
            or not 1 <= pagination["limit"] <= 100
        ):
            errors.append("response.pagination.limit must be an integer from 1 through 100")
        if pagination.get("next_cursor") is not None and not isinstance(
            pagination.get("next_cursor"), str
        ):
            errors.append("response.pagination.next_cursor must be a string or null")
    _validate_freshness(response.get("freshness"), "response.freshness", errors)
    return errors


def validate_health_response(value: Any) -> list[str]:
    """Validate the success body of ``GET /v1/health``."""
    errors: list[str] = []
    response = _mapping(value, "response", errors)
    if response is None:
        return errors
    _validate_version(response, errors)
    health = _mapping(response.get("data"), "response.data", errors)
    if health is not None:
        _required_keys(health, HEALTH_REQUIRED_KEYS, "response.data", errors)
        if "checked_at" in health and not _is_timestamp(health["checked_at"]):
            errors.append("response.data.checked_at must be an RFC 3339 timestamp")
        if health.get("status") not in {"healthy", "degraded", "unavailable"}:
            errors.append("response.data.status must be healthy, degraded, or unavailable")
        _validate_freshness(health.get("freshness"), "response.data.freshness", errors)
        stages = health.get("stages")
        if not isinstance(stages, Mapping):
            errors.append("response.data.stages must be an object")
        else:
            for name, stage in stages.items():
                stage_path = f"response.data.stages.{name}"
                stage_value = _mapping(stage, stage_path, errors)
                if stage_value is not None:
                    _string(stage_value.get("health"), f"{stage_path}.health", errors)
                    _string(stage_value.get("circuit_state"), f"{stage_path}.circuit_state", errors)
    return errors


def validate_error_response(value: Any) -> list[str]:
    """Validate a non-success response body shared by all endpoints."""
    errors: list[str] = []
    response = _mapping(value, "response", errors)
    if response is None:
        return errors
    _validate_version(response, errors)
    _required_keys(response, ERROR_REQUIRED_KEYS, "response", errors)
    detail = _mapping(response.get("error"), "response.error", errors)
    if detail is not None:
        _required_keys(detail, ERROR_DETAIL_REQUIRED_KEYS, "response.error", errors)
        if detail.get("code") not in ERROR_CODES:
            errors.append("response.error.code must be a stable contract error code")
        _string(detail.get("message"), "response.error.message", errors)
    return errors


__all__ = [
    "API_VERSION",
    "ERROR_CODES",
    "HEALTH_PATH",
    "HISTORICAL_FORECASTS_PATH",
    "LATEST_FORECAST_PATH",
    "MEDIA_TYPE",
    "validate_error_response",
    "validate_forecast",
    "validate_health_response",
    "validate_historical_response",
    "validate_latest_response",
]
