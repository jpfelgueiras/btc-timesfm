#!/usr/bin/env python3
"""Production integration for timestamp-safe derivatives context.

This module deliberately wraps the existing production entrypoints rather than
changing the forecast model itself. Derivatives values are captured at the
completed-candle origin, persisted with market features, and exposed in
forecast.json, but they do not alter predictions until research demonstrates an
out-of-sample edge.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from derivatives_signals import fetch_derivatives_snapshot, signal_manifest


SNAPSHOT_PATH = Path("derivatives_signal.json")


def _market_data(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    if len(args) >= 2:
        return args[1]
    data = kwargs.get("data")
    if data is None:
        raise RuntimeError("build_forecast call did not provide market data")
    return data


def activate_derivatives_runtime(target: Any) -> None:
    """Attach passive derivatives capture to a btc_forecast-like module."""
    if getattr(target, "_derivatives_runtime_wrapped", False):
        return

    original_build_forecast = target.build_forecast
    original_manifest = target.build_experiment_manifest

    def build_with_derivatives(*args: Any, **kwargs: Any) -> dict[str, Any]:
        data = _market_data(args, kwargs)
        origin = datetime.fromtimestamp(int(data.timestamps[-1]), tz=timezone.utc)
        snapshot = fetch_derivatives_snapshot(origin)
        target._latest_derivatives_snapshot = snapshot
        SNAPSHOT_PATH.write_text(
            json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        result = original_build_forecast(*args, **kwargs)
        if not isinstance(result, dict):
            raise TypeError("build_forecast must return a dictionary")

        raw_features = snapshot.get("features")
        market_features = result.get("market_features")
        if isinstance(raw_features, dict) and isinstance(market_features, dict):
            for name, value in raw_features.items():
                if (
                    isinstance(name, str)
                    and isinstance(value, (int, float))
                    and not isinstance(value, bool)
                ):
                    market_features[name] = float(value)

        # btc_forecast spreads the engine output into forecast.json, so keeping
        # this key here exposes provenance/quality without changing core APIs.
        result["derivatives_signals"] = snapshot
        return result

    def manifest_with_derivatives(*args: Any, **kwargs: Any) -> dict[str, Any]:
        run_parameters = kwargs.get("run_parameters")
        parameters = dict(run_parameters) if isinstance(run_parameters, dict) else {}
        snapshot = getattr(target, "_latest_derivatives_snapshot", None)
        if isinstance(snapshot, dict):
            parameters["derivatives_signals"] = signal_manifest(snapshot)
        kwargs["run_parameters"] = parameters
        manifest = original_manifest(*args, **kwargs)
        if not isinstance(manifest, dict):
            raise TypeError("build_experiment_manifest must return a dictionary")
        return manifest

    target.build_forecast = build_with_derivatives
    target.build_experiment_manifest = manifest_with_derivatives
    target._derivatives_runtime_wrapped = True
