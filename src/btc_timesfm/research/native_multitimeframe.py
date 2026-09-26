"""Causal data gate and bounded report for native multi-timeframe forecasts.

This module deliberately does not invoke TimesFM or infer frequency metadata.
It validates completed UTC bars and exact target intersections; unavailable corpus
or runtime frequency support leaves the experiment blocked with no accuracy claim.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Mapping

FIFTEEN_MINUTES = 900
FIVE_MINUTES = 300


def aggregate_ohlcv_15m(candles: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Aggregate complete contiguous 5m candles into UTC 15m close-stamped bars."""
    columns = ("timestamps", "opens", "highs", "lows", "closes", "volumes")
    if any(key not in candles for key in columns):
        raise ValueError("OHLCV columns are required")
    values = {key: list(candles[key]) for key in columns}
    size = len(values["timestamps"])
    if any(len(values[key]) != size for key in columns[1:]):
        raise ValueError("OHLCV columns must have equal lengths")
    rows: dict[int, dict[str, float]] = {}
    previous = None
    for index, raw_ts in enumerate(values["timestamps"]):
        ts = int(raw_ts)
        if ts % FIVE_MINUTES:
            raise ValueError("5m candle timestamps must align to UTC boundaries")
        if previous is not None and ts <= previous:
            raise ValueError("5m candle timestamps must be strictly increasing")
        previous = ts
        row = {
            name: float(values[column][index])
            for column, name in zip(columns[1:], ("open", "high", "low", "close", "volume"))
        }
        if not all(math.isfinite(value) for value in row.values()):
            raise ValueError("OHLCV values must be finite")
        if min(row["open"], row["high"], row["low"], row["close"]) <= 0 or row["volume"] < 0:
            raise ValueError("prices must be positive and volume nonnegative")
        if row["low"] > min(row["open"], row["close"]) or row["high"] < max(
            row["open"], row["close"]
        ):
            raise ValueError("invalid OHLC relationship")
        bucket = ts // FIFTEEN_MINUTES * FIFTEEN_MINUTES
        rows.setdefault(bucket, {})[str(index)] = row

    output = []
    for start, indexed in sorted(rows.items()):
        group = [indexed[key] for key in sorted(indexed, key=int)]
        expected = [start, start + FIVE_MINUTES, start + 2 * FIVE_MINUTES]
        actual = [int(values["timestamps"][int(key)]) for key in sorted(indexed, key=int)]
        if actual != expected:
            continue
        output.append(
            {
                "timestamp": start + FIFTEEN_MINUTES,
                "open": group[0]["open"],
                "high": max(row["high"] for row in group),
                "low": min(row["low"] for row in group),
                "close": group[-1]["close"],
                "volume": sum(row["volume"] for row in group),
            }
        )
    return output


def exact_target_matches(
    hourly: Mapping[int, float], native: Mapping[int, float]
) -> list[dict[str, float | int]]:
    """Return only identical UTC target-close timestamps present in both outputs."""
    return [
        {
            "target_ts": timestamp,
            "hourly": float(hourly[timestamp]),
            "native": float(native[timestamp]),
        }
        for timestamp in sorted(set(hourly) & set(native))
    ]


def score_exact_targets(
    hourly: Mapping[int, float],
    native: Mapping[int, float],
    actual: Mapping[int, float],
    *,
    hourly_origins: Mapping[int, int] | None = None,
    native_origins: Mapping[int, int] | None = None,
) -> dict[str, Any]:
    """Score forecast pairs only where both models share target and origin, and truth exists."""
    common = set(hourly) & set(native) & set(actual)
    if hourly_origins is not None or native_origins is not None:
        if hourly_origins is None or native_origins is None:
            common.clear()
        else:
            common = {
                target
                for target in common
                if target in hourly_origins
                and target in native_origins
                and hourly_origins[target] == native_origins[target]
            }
    pairs = [
        (float(hourly[target]), float(native[target]), float(actual[target]))
        for target in sorted(common)
        if all(
            math.isfinite(float(value))
            for value in (hourly[target], native[target], actual[target])
        )
    ]
    hourly_residuals = [prediction - truth for prediction, _, truth in pairs]
    native_residuals = [prediction - truth for _, prediction, truth in pairs]

    def losses(residuals: list[float]) -> dict[str, float | None]:
        if not residuals:
            return {"mae": None, "mse": None}
        return {
            "mae": sum(abs(value) for value in residuals) / len(residuals),
            "mse": sum(value * value for value in residuals) / len(residuals),
        }

    correlation: float | None = None
    if len(pairs) >= 2:
        mean_hourly = sum(hourly_residuals) / len(pairs)
        mean_native = sum(native_residuals) / len(pairs)
        covariance = sum(
            (left - mean_hourly) * (right - mean_native)
            for left, right in zip(hourly_residuals, native_residuals)
        )
        hourly_ss = sum((value - mean_hourly) ** 2 for value in hourly_residuals)
        native_ss = sum((value - mean_native) ** 2 for value in native_residuals)
        if hourly_ss > 0 and native_ss > 0:
            correlation = covariance / math.sqrt(hourly_ss * native_ss)
    return {
        "matched_targets": len(pairs),
        "eligible_hourly_count": len(pairs),
        "eligible_native_count": len(pairs),
        "hourly_losses": losses(hourly_residuals),
        "native_losses": losses(native_residuals),
        "residual_correlation": correlation,
    }


def build_report(
    *,
    corpus_available: bool = False,
    runtime_frequency_supported: bool = False,
    corpus_contract: Mapping[str, Any] | None = None,
    runtime_contract: Mapping[str, Any] | None = None,
    scoring: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    blockers = []
    verified_corpus = bool(
        corpus_contract
        and corpus_contract.get("immutable") is True
        and corpus_contract.get("venue")
        and corpus_contract.get("symbol") == "BTC/USD"
        and corpus_contract.get("frequency") == "15m"
    )
    verified_runtime = bool(
        runtime_contract
        and runtime_contract.get("model") == "TimesFM"
        and runtime_contract.get("frequency") == "15m"
        and runtime_contract.get("supported") is True
    )
    if not corpus_available or not verified_corpus:
        blockers.append("immutable same-venue 15m BTC/USD corpus unavailable")
    if not runtime_frequency_supported or not verified_runtime:
        blockers.append("TimesFM runtime frequency contract for 15m is unavailable")
    def has_losses(value: Any) -> bool:
        return isinstance(value, Mapping) and all(
            key in value
            and isinstance(value[key], (int, float))
            and math.isfinite(value[key])
            and value[key] >= 0
            for key in ("mae", "mse")
        )

    valid_scoring = bool(
        scoring
        and isinstance(scoring.get("matched_targets"), int)
        and scoring["matched_targets"] >= 2
        and scoring.get("eligible_hourly_count") == scoring.get("matched_targets")
        and scoring.get("eligible_native_count") == scoring.get("matched_targets")
        and has_losses(scoring.get("hourly_losses"))
        and has_losses(scoring.get("native_losses"))
    )
    if not valid_scoring:
        blockers.append("at least two exact matched forecast pairs with scoring output are required")
    return {
        "schema_version": 1,
        "status": "ready_for_scoring" if not blockers else "blocked",
        "blockers": blockers,
        "comparison": "native 1h versus native 15m on identical UTC target closes",
        "optional_4h_targets": ["4h", "8h", "16h"],
        "summary_ridge_control": "separate control; never treated as native TimesFM",
        "blend": "not evaluated; standalone skill and residual dependence precede inner-OOF convex weighting",
        "matched_targets": scoring.get("matched_targets", 0) if scoring else 0,
        "scoring": dict(scoring) if scoring else None,
        "accuracy_claim": None,
        "accuracy_claim_note": "Blocked/unavailable evidence is not a negative accuracy result.",
        "production_changed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("native_multitimeframe_report.json"))
    args = parser.parse_args()
    report = build_report()
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
