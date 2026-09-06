#!/usr/bin/env python3
from pathlib import Path

# Triggered after the temporary workflow was present on the branch.


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"expected text not found in {path}: {old[:80]!r}")
    file.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "btc_forecast.py",
    "from drift_detection import evaluate_drift, persist_drift_report\n",
    "from drift_detection import evaluate_drift, persist_drift_report\n"
    "from derivatives_signals import fetch_derivatives_snapshot, signal_manifest\n",
)
replace_once(
    "btc_forecast.py",
    'HISTORY_DB_PATH = DEFAULT_DB_PATH\nHISTORY_LIMIT = 72\n',
    'HISTORY_DB_PATH = DEFAULT_DB_PATH\nHISTORY_LIMIT = 72\nDERIVATIVES_PATH = Path("derivatives_signal.json")\n',
)
replace_once(
    "btc_forecast.py",
    '        "market_data_provenance": output.get("market_data_provenance"),\n'
    '        "experiment_manifest": output.get("experiment_manifest"),\n',
    '        "market_data_provenance": output.get("market_data_provenance"),\n'
    '        "derivatives_signals": output.get("derivatives_signals"),\n'
    '        "experiment_manifest": output.get("experiment_manifest"),\n',
)
replace_once(
    "btc_forecast.py",
    '    actuals = dict(zip(data.timestamps, map(float, data.closes), strict=True))\n'
    '    rolling_history = load_forecast_history()\n',
    '    forecast_origin = datetime.fromtimestamp(data.timestamps[-1], tz=timezone.utc)\n'
    '    derivatives_snapshot = fetch_derivatives_snapshot(forecast_origin)\n'
    '    DERIVATIVES_PATH.write_text(\n'
    '        json.dumps(derivatives_snapshot, indent=2, sort_keys=True) + "\\n", encoding="utf-8"\n'
    '    )\n'
    '    print(\n'
    '        f"Derivatives signals: {derivatives_snapshot[\'status\']} | "\n'
    '        f"features={len(derivatives_snapshot.get(\'features\', {}))}"\n'
    '    )\n\n'
    '    actuals = dict(zip(data.timestamps, map(float, data.closes), strict=True))\n'
    '    rolling_history = load_forecast_history()\n',
)
replace_once(
    "btc_forecast.py",
    '    engine_output = build_forecast(model, data, history, adaptive_confidence=adaptive_confidence)\n'
    '    interval_calibration_evaluation = evaluation_report(\n',
    '    engine_output = build_forecast(model, data, history, adaptive_confidence=adaptive_confidence)\n'
    '    derivative_features = derivatives_snapshot.get("features", {})\n'
    '    market_features = engine_output.get("market_features")\n'
    '    if isinstance(market_features, dict) and isinstance(derivative_features, dict):\n'
    '        for name, value in derivative_features.items():\n'
    '            if isinstance(name, str) and isinstance(value, (int, float)) and not isinstance(value, bool):\n'
    '                market_features[name] = float(value)\n'
    '    interval_calibration_evaluation = evaluation_report(\n',
)
replace_once(
    "btc_forecast.py",
    '            "drift_configuration": drift_report["configuration"],\n'
    '        },\n',
    '            "drift_configuration": drift_report["configuration"],\n'
    '            "derivatives_signals": signal_manifest(derivatives_snapshot),\n'
    '        },\n',
)
replace_once(
    "btc_forecast.py",
    '        "experiment_manifest": experiment_manifest,\n'
    '        "drift_detection": drift_report,\n',
    '        "experiment_manifest": experiment_manifest,\n'
    '        "derivatives_signals": derivatives_snapshot,\n'
    '        "drift_detection": drift_report,\n',
)

Path("test_derivatives_ablation.py").write_text(
    '''#!/usr/bin/env python3
"""Tests for leakage-safe derivatives feature ablation."""

from __future__ import annotations

import unittest

from derivatives_ablation import eligible_training_rows, walk_forward_ablation


class DerivativesAblationTests(unittest.TestCase):
    def test_training_rows_require_matured_target(self) -> None:
        rows = [
            {"origin_timestamp": 1000},
            {"origin_timestamp": 1000 + 3600},
            {"origin_timestamp": 1000 + 2 * 3600},
        ]
        eligible = eligible_training_rows(rows, 1000 + 4 * 3600, 2)
        self.assertEqual([row["origin_timestamp"] for row in eligible], [1000, 4600, 8200])
        eligible = eligible_training_rows(rows, 1000 + 3 * 3600, 2)
        self.assertEqual([row["origin_timestamp"] for row in eligible], [1000, 4600])

    def test_ablation_report_is_deterministic_and_no_lookahead(self) -> None:
        rows = []
        for index in range(40):
            base = float(index) / 100.0
            rows.append(
                {
                    "origin_at": f"2026-01-{index // 24 + 1:02d}T{index % 24:02d}:00:00+00:00",
                    "origin_timestamp": index * 3600,
                    "current_price": 100.0 + index,
                    "market_features": [base, base * 2, 0.1, 50.0, base, base * 3, base * 4],
                    "derivatives_features": [
                        0.01,
                        1_000_000.0 + index * 1000,
                        base,
                        base * 2,
                        10.0,
                        20.0,
                        30.0,
                        1.0 / 3.0,
                    ],
                    "targets": {
                        "2h": 0.001 + base / 100,
                        "4h": 0.002 + base / 100,
                        "8h": 0.003 + base / 100,
                        "16h": 0.004 + base / 100,
                    },
                }
            )
        first = walk_forward_ablation(rows, min_train=8)
        second = walk_forward_ablation(rows, min_train=8)
        self.assertEqual(first, second)
        self.assertFalse(first["uses_future_information"])
        self.assertEqual(first["method"], "paired_leakage_safe_walk_forward_ridge_ablation")
        for horizon, item in first["by_horizon"].items():
            self.assertGreater(item["market_only"]["samples"], 0, horizon)
            self.assertEqual(item["market_only"]["samples"], item["market_plus_derivatives"]["samples"])
            self.assertEqual(len(item["origins"]), item["market_only"]["samples"])


if __name__ == "__main__":
    unittest.main()
''',
    encoding="utf-8",
)

Path("test_derivatives_history_integration.py").write_text(
    '''#!/usr/bin/env python3
"""Durable-history integration tests for derivatives feature snapshots."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from history_store import ForecastHistoryStore


class DerivativesHistoryIntegrationTests(unittest.TestCase):
    def test_normalized_raw_and_derived_features_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ForecastHistoryStore(Path(directory) / "history.sqlite")
            origin = datetime(2026, 9, 6, 8, tzinfo=timezone.utc)
            derivative_features = {
                "derivatives_funding_rate_pct": 0.01,
                "derivatives_open_interest_usd": 1_200_000.0,
                "derivatives_oi_change_1h_pct": 2.5,
                "derivatives_oi_change_24h_pct": -3.0,
                "derivatives_long_liquidation_usd_1h": 100.0,
                "derivatives_short_liquidation_usd_1h": 300.0,
                "derivatives_liquidation_total_usd_1h": 400.0,
                "derivatives_liquidation_imbalance": 0.5,
            }
            snapshot = {
                "generated_at": origin.isoformat(),
                "latest_close_at": origin.isoformat(),
                "latest_close_usd": 60_000.0,
                "source": "Kraken hourly OHLC",
                "pair": "BTC/USD",
                "regime": "range",
                "market_features": {"rsi_14": 50.0, **derivative_features},
                "model_weights": {},
                "model_predictions": {},
                "predictions": {
                    "2h": {
                        "price_usd": 60_100.0,
                        "change_pct": 100.0 / 60_000.0 * 100.0,
                        "q10_usd": 59_000.0,
                        "q90_usd": 61_000.0,
                    }
                },
            }
            store.ingest_snapshot(snapshot)
            loaded = store.load_snapshots()[0]
            for name, value in derivative_features.items():
                self.assertEqual(loaded["market_features"][name], value)
            exported = store.export_rows()[0]
            self.assertIn("derivatives_open_interest_usd", exported["market_features_json"])


if __name__ == "__main__":
    unittest.main()
''',
    encoding="utf-8",
)

Path("DERIVATIVES_SIGNALS.md").write_text(
    '''# Crypto derivatives signals

Issue #34 adds timestamp-safe BTC derivatives context without making the production forecast depend on an external derivatives provider.

## Providers

- **Funding rate:** Binance USD-M `BTCUSDT` perpetual funding history.
- **Open interest and liquidations:** Gate USDT `BTC_USDT` perpetual contract statistics.

Both APIs are public and require no repository secret. Provider failures are isolated: the spot forecast continues with a `partial` or `unavailable` derivatives status.

## Timestamp and freshness rules

Every snapshot is bounded to the latest completed spot-candle timestamp. Rows after that forecast origin are discarded before any feature is derived. Funding older than 12 hours and contract statistics older than 2.5 hours are treated as stale and omitted.

The normalized feature set is:

- `derivatives_funding_rate_pct`
- `derivatives_open_interest_usd`
- `derivatives_oi_change_1h_pct`
- `derivatives_oi_change_24h_pct`
- `derivatives_long_liquidation_usd_1h`
- `derivatives_short_liquidation_usd_1h`
- `derivatives_liquidation_total_usd_1h`
- `derivatives_liquidation_imbalance`

The funding/open-interest/liquidation values are normalized raw measurements. OI changes, liquidation total, and imbalance are derived values. Available values are merged into `market_features` and therefore persist with the immutable forecast origin in durable SQLite history. `forecast.json` also retains provider provenance, freshness, missing-feature diagnostics, and the latest bounded provider rows.

Production does **not** change ensemble weights or predictions merely because derivatives data is available. This avoids promoting a signal before out-of-sample evidence exists.

## Walk-forward ablation

`derivatives_ablation.py` compares the same ridge forecaster with two feature sets:

1. current spot-market features only;
2. the exact same features plus all derivatives features.

For every simulated origin, a training row is eligible only when its target timestamp is already observable for the horizon being evaluated. The report includes MAE, bias, direction accuracy, paired statistical evidence, and a conservative `edge_detected` / `no_defensible_edge` recommendation for 2h, 4h, 8h, and 16h.

Run locally with:

```bash
python derivatives_ablation.py --days 30 --samples 96 --min-train 48
```

The scheduled/manual GitHub Actions workflow publishes `derivatives_ablation_report.json` and `derivatives_ablation_summary.md`. This research result is evidence for later feature-selection work; it does not automatically alter production.
''',
    encoding="utf-8",
)
