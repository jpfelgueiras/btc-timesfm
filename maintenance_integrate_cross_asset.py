#!/usr/bin/env python3
from pathlib import Path

path = Path("btc_forecast.py")
text = path.read_text(encoding="utf-8")


def replace_once(old: str, new: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Expected exactly one match, found {count}: {old[:120]!r}")
    text = text.replace(old, new, 1)


replace_once(
    "from conformal_calibration import conformal_calibration_multiplier, evaluation_report\n",
    "from conformal_calibration import conformal_calibration_multiplier, evaluation_report\n"
    "from cross_asset_signals import (\n"
    "    fetch_cross_asset_snapshot,\n"
    "    signal_manifest as cross_asset_manifest,\n"
    ")\n",
)
replace_once(
    'MICROSTRUCTURE_PATH = Path("microstructure_signal.json")\n',
    'MICROSTRUCTURE_PATH = Path("microstructure_signal.json")\n'
    'CROSS_ASSET_PATH = Path("cross_asset_signal.json")\n',
)
replace_once(
    '        "microstructure_signals": output.get("microstructure_signals"),\n',
    '        "microstructure_signals": output.get("microstructure_signals"),\n'
    '        "cross_asset_signals": output.get("cross_asset_signals"),\n',
)
replace_once(
    "    print(\n"
    "        f\"Microstructure signals: {microstructure_snapshot['status']} | \"\n"
    "        f\"features={len(microstructure_snapshot.get('features', {}))}\"\n"
    "    )\n\n"
    "    actuals = dict(zip(data.timestamps, map(float, data.closes), strict=True))\n",
    "    print(\n"
    "        f\"Microstructure signals: {microstructure_snapshot['status']} | \"\n"
    "        f\"features={len(microstructure_snapshot.get('features', {}))}\"\n"
    "    )\n"
    "    cross_asset_snapshot = fetch_cross_asset_snapshot(forecast_origin, data)\n"
    "    CROSS_ASSET_PATH.write_text(\n"
    "        json.dumps(cross_asset_snapshot, indent=2, sort_keys=True) + \"\\n\",\n"
    "        encoding=\"utf-8\",\n"
    "    )\n"
    "    print(\n"
    "        f\"Cross-asset signals: {cross_asset_snapshot['status']} | \"\n"
    "        f\"features={len(cross_asset_snapshot.get('features', {}))}\"\n"
    "    )\n\n"
    "    actuals = dict(zip(data.timestamps, map(float, data.closes), strict=True))\n",
)
replace_once(
    '    microstructure_features = microstructure_snapshot.get("features", {})\n'
    '    market_features = engine_output.get("market_features")\n'
    '    if isinstance(market_features, dict):\n'
    '        for feature_group in (derivative_features, microstructure_features):\n',
    '    microstructure_features = microstructure_snapshot.get("features", {})\n'
    '    cross_asset_features = cross_asset_snapshot.get("features", {})\n'
    '    market_features = engine_output.get("market_features")\n'
    '    if isinstance(market_features, dict):\n'
    '        for feature_group in (\n'
    '            derivative_features,\n'
    '            microstructure_features,\n'
    '            cross_asset_features,\n'
    '        ):\n',
)
replace_once(
    '            "microstructure_signals": microstructure_manifest(microstructure_snapshot),\n',
    '            "microstructure_signals": microstructure_manifest(microstructure_snapshot),\n'
    '            "cross_asset_signals": cross_asset_manifest(cross_asset_snapshot),\n',
)
replace_once(
    '        "microstructure_signals": microstructure_snapshot,\n',
    '        "microstructure_signals": microstructure_snapshot,\n'
    '        "cross_asset_signals": cross_asset_snapshot,\n',
)

path.write_text(text, encoding="utf-8")
