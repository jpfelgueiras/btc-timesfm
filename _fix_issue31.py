from pathlib import Path


def replace(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"expected text not found in {path}: {old[:100]!r}")
    file.write_text(text.replace(old, new, 1), encoding="utf-8")


replace(
    "adaptive_weighting.py",
    '        model_name = str(row.get("model_name"))\n        try:\n',
    '        model_name = str(row.get("model_name"))\n'
    '        if model_name == "ensemble":\n'
    '            continue\n'
    '        try:\n',
)

replace(
    "conformal_calibration.py",
    '''    try:\n        value = float(snapshot["_outcomes"][horizon]["ensemble"]["actual_target_price_usd"])\n        if value > 0:\n            return value\n    except (KeyError, TypeError, ValueError):\n        pass\n    origin = _origin(snapshot)\n    if origin is None:\n        return None\n    value = actual_by_timestamp.get(int(origin.timestamp()) + hour * 3600)\n    if value is None:\n        return None\n    value = float(value)\n    return value if value > 0 else None\n''',
    '''    try:\n        outcomes = snapshot["_outcomes"][horizon]\n        for outcome in outcomes.values():\n            value = float(outcome["actual_target_price_usd"])\n            if value > 0:\n                return value\n    except (KeyError, AttributeError, TypeError, ValueError):\n        pass\n    origin = _origin(snapshot)\n    if origin is None:\n        return None\n    candle_actual = actual_by_timestamp.get(int(origin.timestamp()) + hour * 3600)\n    if candle_actual is None:\n        return None\n    value = float(candle_actual)\n    return value if value > 0 else None\n''',
)

replace(
    "conformal_calibration.py",
    '''def conformal_calibration_multiplier(\n    history: list[dict[str, Any]],\n    actual_by_timestamp: dict[int, float],\n    hour: int,\n    target_coverage: float = DEFAULT_TARGET_COVERAGE,\n) -> tuple[float, int, float | None]:\n    """Compatibility adapter for forecast_engine's interval-calibration hook."""\n    details = calibration_details(\n        history,\n        actual_by_timestamp,\n        hour,\n        target_coverage=target_coverage,\n    )\n    return (\n        float(details["multiplier"]),\n        int(details["samples"]),\n        details["empirical_coverage_after"],\n    )\n''',
    '''def _legacy_interval_only(\n    history: list[dict[str, Any]],\n    actual_by_timestamp: dict[int, float],\n    hour: int,\n    target_coverage: float,\n) -> tuple[float, int, float | None]:\n    horizon = f"{hour}h"\n    covered: list[bool] = []\n    for snapshot in reversed(history):\n        try:\n            origin = _origin(snapshot)\n            if origin is None:\n                continue\n            item = snapshot["predictions"][horizon]\n            q10 = float(item["q10_usd"])\n            q90 = float(item["q90_usd"])\n            actual = actual_by_timestamp.get(int(origin.timestamp()) + hour * 3600)\n            if actual is None:\n                continue\n            covered.append(q10 <= float(actual) <= q90)\n        except (KeyError, TypeError, ValueError):\n            continue\n        if len(covered) >= 48:\n            break\n    samples = len(covered)\n    if samples < 10:\n        return 1.0, samples, None\n    coverage = float(np.mean(covered))\n    multiplier = math.sqrt(target_coverage / max(coverage, 0.10))\n    return float(np.clip(multiplier, 0.75, 1.75)), samples, coverage\n\n\ndef conformal_calibration_multiplier(\n    history: list[dict[str, Any]],\n    actual_by_timestamp: dict[int, float],\n    hour: int,\n    target_coverage: float = DEFAULT_TARGET_COVERAGE,\n) -> tuple[float, int, float | None]:\n    """Compatibility adapter for forecast_engine's interval-calibration hook."""\n    details = calibration_details(\n        history,\n        actual_by_timestamp,\n        hour,\n        target_coverage=target_coverage,\n    )\n    if int(details["samples"]) == 0:\n        return _legacy_interval_only(history, actual_by_timestamp, hour, target_coverage)\n    return (\n        float(details["multiplier"]),\n        int(details["samples"]),\n        details["empirical_coverage_after"],\n    )\n''',
)

Path("_fix_issue31.py").unlink(missing_ok=True)
