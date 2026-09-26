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


def build_report(
    *, corpus_available: bool = False, runtime_frequency_supported: bool = False
) -> dict[str, Any]:
    blockers = []
    if not corpus_available:
        blockers.append("immutable same-venue 15m BTC/USD corpus unavailable")
    if not runtime_frequency_supported:
        blockers.append("TimesFM runtime frequency contract for 15m is unavailable")
    return {
        "schema_version": 1,
        "status": "blocked" if blockers else "ready_for_scoring",
        "blockers": blockers,
        "comparison": "native 1h versus native 15m on identical UTC target closes",
        "optional_4h_targets": ["4h", "8h", "16h"],
        "summary_ridge_control": "separate control; never treated as native TimesFM",
        "blend": "not evaluated; standalone skill and residual dependence precede inner-OOF convex weighting",
        "matched_targets": 0,
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
