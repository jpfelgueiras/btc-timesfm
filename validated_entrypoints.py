#!/usr/bin/env python3
"""Validated entrypoints for production forecasts, backtests, and optimization.

This keeps validation at the edge of every automated pipeline without coupling
TimesFM model code to provider-specific data-quality policy.
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


def run_forecast(argv: list[str]) -> None:
    if argv:
        raise SystemExit("forecast does not accept positional arguments")
    import btc_forecast

    # Production provider selection performs issue #17 validation for both
    # Kraken and the Binance fallback before btc_forecast sees the data.
    btc_forecast.main()


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
