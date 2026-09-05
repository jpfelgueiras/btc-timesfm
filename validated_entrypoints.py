#!/usr/bin/env python3
"""Validated entrypoints for production forecasts, backtests, and optimization.

This keeps validation at the edge of every automated pipeline without coupling
TimesFM model code to provider-specific data-quality policy. Production forecast
execution is also instrumented here so data, model and history stages emit a
shared structured observability record without coupling core model code to CI.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any, Callable

from market_data_validation import (
    MarketDataValidationError,
    ValidationConfig,
    persist_validation_report,
    print_validation_report,
    trim_incomplete_trailing_candles,
    validate_market_data,
)
from observability import PipelineObserver


def _validate_and_persist(
    data: Any,
    *,
    source: str,
    check_staleness: bool,
    trim_incomplete: bool = False,
) -> Any:
    removed = (
        trim_incomplete_trailing_candles(data, now=datetime.now(timezone.utc))
        if trim_incomplete
        else 0
    )
    try:
        report = validate_market_data(
            data,
            source=source,
            now=datetime.now(timezone.utc),
            config=ValidationConfig.from_env(),
            check_staleness=check_staleness,
        )
    except MarketDataValidationError as exc:
        if removed:
            exc.report.metrics["trimmed_incomplete_candles"] = removed
        persist_validation_report(exc.report)
        print_validation_report(exc.report)
        raise
    if removed:
        report.metrics["trimmed_incomplete_candles"] = removed
    persist_validation_report(report)
    print_validation_report(report)
    return data


def _instrument_forecast(observer: PipelineObserver, btc_forecast: Any) -> None:
    original_fetch = btc_forecast.fetch_redundant_hourly
    original_load_model = btc_forecast.load_timesfm
    original_build_forecast = btc_forecast.build_forecast
    original_drift = btc_forecast.evaluate_production_drift
    original_manifest = btc_forecast.build_experiment_manifest
    original_store = btc_forecast.ForecastHistoryStore

    def fetch_observed(limit: int = 512):
        with observer.stage("market_data_fetch", requested_candles=limit, includes_validation=True):
            selection = original_fetch(limit)

        selected = selection.secondary if selection.fallback_used else selection.primary
        validation_metrics = selected.validation.metrics if selected.validation is not None else {}
        soft_warnings = validation_metrics.get("soft_validation_warnings", [])
        observer.record_stage(
            "market_data_validation",
            status="success",
            provider=selection.provider,
            source_pair=selection.source_pair,
            fallback_used=selection.fallback_used,
            soft_warning_count=len(soft_warnings) if isinstance(soft_warnings, list) else 0,
            comparison_status=(selection.comparison or {}).get("status"),
        )
        observer.metadata(
            market_data_provider=selection.provider,
            market_data_pair=selection.source_pair,
            market_data_fallback=selection.fallback_used,
        )
        if selection.fallback_used:
            observer.increment(
                "fallbacks",
                provider=selection.provider,
                primary_error=selection.primary.error,
            )
        quality_events = len(soft_warnings) if isinstance(soft_warnings, list) else 0
        if selection.primary.validation is not None and not selection.primary.healthy:
            quality_events += 1
        if quality_events:
            observer.increment("data_quality_events", quality_events, provider=selection.provider)
        observer.event(
            "market_data_selected",
            status="success",
            provider=selection.provider,
            fallback_used=selection.fallback_used,
            primary_healthy=selection.primary.healthy,
            secondary_healthy=selection.secondary.healthy,
            comparison=selection.comparison,
        )
        return selection

    def load_model_observed(*args: Any, **kwargs: Any):
        with observer.stage("model_load", model="timesfm"):
            return original_load_model(*args, **kwargs)

    def build_forecast_observed(*args: Any, **kwargs: Any):
        with observer.stage("model_inference", model="timesfm_ensemble"):
            return original_build_forecast(*args, **kwargs)

    def drift_observed(*args: Any, **kwargs: Any):
        with observer.stage("drift_detection"):
            report = original_drift(*args, **kwargs)
        severity = str(report.get("severity", "none"))
        if severity == "warning":
            observer.increment("drift_warnings")
        elif severity == "severe":
            observer.increment("drift_severe")
        observer.event(
            "drift_evaluated",
            status="success",
            severity=severity,
            adaptive_confidence=report.get("adaptive_confidence"),
            event_count=report.get("summary", {}).get("events"),
            fallback_mode=report.get("fallback_mode"),
        )
        return report

    def manifest_observed(*args: Any, **kwargs: Any):
        manifest = original_manifest(*args, **kwargs)
        observer.set_experiment_id(manifest.get("run_id"))
        observer.metadata(
            configuration_id=manifest.get("configuration_id"),
            data_id=manifest.get("data_id"),
        )
        return manifest

    class ObservedForecastHistoryStore(original_store):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            with observer.stage("history_open"):
                super().__init__(*args, **kwargs)

        def ingest_snapshots(self, *args: Any, **kwargs: Any):
            with observer.stage("history_bootstrap_persistence"):
                return super().ingest_snapshots(*args, **kwargs)

        def load_snapshots(self, *args: Any, **kwargs: Any):
            with observer.stage("history_read"):
                return super().load_snapshots(*args, **kwargs)

        def performance_summary(self, *args: Any, **kwargs: Any):
            with observer.stage("history_metrics"):
                return super().performance_summary(*args, **kwargs)

        def ingest_snapshot(self, *args: Any, **kwargs: Any):
            with observer.stage("history_persistence"):
                return super().ingest_snapshot(*args, **kwargs)

        def record_drift_events(self, *args: Any, **kwargs: Any):
            with observer.stage("drift_persistence"):
                return super().record_drift_events(*args, **kwargs)

        def verify(self, *args: Any, **kwargs: Any):
            with observer.stage("history_verify"):
                return super().verify(*args, **kwargs)

    btc_forecast.fetch_redundant_hourly = fetch_observed
    btc_forecast.load_timesfm = load_model_observed
    btc_forecast.build_forecast = build_forecast_observed
    btc_forecast.evaluate_production_drift = drift_observed
    btc_forecast.build_experiment_manifest = manifest_observed
    btc_forecast.ForecastHistoryStore = ObservedForecastHistoryStore


def run_forecast(argv: list[str]) -> None:
    if argv:
        raise SystemExit("forecast does not accept positional arguments")
    import btc_forecast

    observer = PipelineObserver(run_type="production_forecast")
    _instrument_forecast(observer, btc_forecast)
    try:
        with observer.stage("forecast_pipeline"):
            btc_forecast.main()
    except BaseException:
        observer.finalize("failed")
        raise
    else:
        observer.finalize("success", preserve_terminal=True)


def _patch_backtest_fetch() -> Any:
    import backtest

    original_fetch = backtest.fetch_binance_history

    def fetch_validated(days: int):
        return _validate_and_persist(
            original_fetch(days),
            source="Binance BTCUSDT hourly klines",
            check_staleness=False,
            trim_incomplete=True,
        )

    backtest.fetch_binance_history = fetch_validated
    return backtest


def run_backtest(argv: list[str]) -> None:
    backtest = _patch_backtest_fetch()
    sys.argv = [sys.argv[0], *argv]
    backtest.main()


def run_optimizer(argv: list[str]) -> None:
    # optimizer imports fetch_binance_history directly from backtest, so patch
    # backtest first and only then import optimizer.
    _patch_backtest_fetch()
    import optimizer

    sys.argv = [sys.argv[0], *argv]
    optimizer.main()


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(
            "Usage: python validated_entrypoints.py {forecast|backtest|optimizer} [arguments]"
        )

    command = sys.argv[1]
    argv = sys.argv[2:]
    commands: dict[str, Callable[[list[str]], None]] = {
        "forecast": run_forecast,
        "backtest": run_backtest,
        "optimizer": run_optimizer,
    }
    try:
        handler = commands[command]
    except KeyError as exc:
        raise SystemExit(f"Unknown command: {command}") from exc
    handler(argv)


if __name__ == "__main__":
    main()
