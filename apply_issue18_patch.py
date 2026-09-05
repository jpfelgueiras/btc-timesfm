#!/usr/bin/env python3
"""One-time source integration patch for issue #18."""

from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Expected exactly one {label} anchor, found {count}")
    return text.replace(old, new, 1)


forecast_path = Path("btc_forecast.py")
forecast = forecast_path.read_text(encoding="utf-8")
forecast = replace_once(
    forecast,
    "from forecast_engine import TARGET_HOURS, build_forecast, fetch_kraken_hourly, load_timesfm\n",
    "from forecast_engine import TARGET_HOURS, build_forecast, load_timesfm\n"
    "from market_data_sources import fetch_redundant_hourly\n",
    "forecast imports",
)
forecast = replace_once(
    forecast,
    '        "pair": output.get("pair"),\n        "regime": output["regime"],',
    '        "pair": output.get("pair"),\n'
    '        "source_pair": output.get("source_pair"),\n'
    '        "market_data_provenance": output.get("market_data_provenance"),\n'
    '        "regime": output["regime"],',
    "rolling provenance",
)
forecast = replace_once(
    forecast,
    'def main() -> None:\n    data = fetch_kraken_hourly(512)\n    print(f"Loaded {len(data.closes)} completed hourly candles")',
    "def main() -> None:\n"
    "    selection = fetch_redundant_hourly(512)\n"
    "    data = selection.data\n"
    '    print(f"Market data source: {selection.source} ({selection.source_pair})")\n'
    '    print(f"Loaded {len(data.closes)} completed hourly candles")',
    "production fetch",
)
forecast = replace_once(
    forecast,
    '        "pair": "BTC/USD",\n        "source": "Kraken hourly OHLC",\n        **engine_output,',
    '        "pair": "BTC/USD",\n'
    '        "source": selection.source,\n'
    '        "source_pair": selection.source_pair,\n'
    '        "market_data_provenance": {\n'
    '            "provider": selection.provider,\n'
    '            "fallback_used": selection.fallback_used,\n'
    '            "source_pair": selection.source_pair,\n'
    '            "comparison": selection.comparison,\n'
    "        },\n"
    "        **engine_output,",
    "forecast provenance",
)
forecast_path.write_text(forecast, encoding="utf-8")

entrypoint_path = Path("validated_entrypoints.py")
entrypoint = entrypoint_path.read_text(encoding="utf-8")
old = """def run_forecast(argv: list[str]) -> None:\n    if argv:\n        raise SystemExit("forecast does not accept positional arguments")\n    import btc_forecast\n\n    original_fetch = btc_forecast.fetch_kraken_hourly\n\n    def fetch_validated(limit: int = 512):\n        return _validate_and_persist(\n            original_fetch(limit),\n            source="Kraken BTC/USD hourly OHLC",\n            check_staleness=True,\n        )\n\n    btc_forecast.fetch_kraken_hourly = fetch_validated\n    btc_forecast.main()\n"""
new = """def run_forecast(argv: list[str]) -> None:\n    if argv:\n        raise SystemExit("forecast does not accept positional arguments")\n    import btc_forecast\n\n    # Production provider selection performs issue #17 validation for both\n    # Kraken and the Binance fallback before btc_forecast sees the data.\n    btc_forecast.main()\n"""
entrypoint = replace_once(entrypoint, old, new, "validated forecast entrypoint")
entrypoint_path.write_text(entrypoint, encoding="utf-8")
