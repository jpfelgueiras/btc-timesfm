"""Audit immutable hourly BTC/USD benchmark drops without substituting venues.

The audit is deliberately a gate, not a backtest: incomplete or mixed-source data
cannot produce a forecast-quality claim. CSV inputs require explicit venue/pair
identity and UTC candle timestamps so coverage and provenance are reproducible.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REQUIRED_WARMUP_DAYS = 180
TARGET_START = datetime(2023, 1, 1, tzinfo=timezone.utc)
TARGET_END = datetime(2026, 9, 1, tzinfo=timezone.utc)
REQUIRED_FIELDS = (
    "timestamp",
    "venue",
    "pair",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "vintage",
    "revision",
)
HOUR_SECONDS = 3600
TARGET_START_TS = int(TARGET_START.timestamp())
TARGET_END_TS = int(TARGET_END.timestamp())
WARMUP_START_TS = TARGET_START_TS - REQUIRED_WARMUP_DAYS * 24 * HOUR_SECONDS


def _utc_timestamp(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a UTC offset")
    parsed = parsed.astimezone(timezone.utc)
    if parsed.minute or parsed.second or parsed.microsecond:
        raise ValueError("timestamps must align to the UTC hour")
    return int(parsed.timestamp())


def audit_csv(path: Path) -> dict[str, Any]:
    """Return a deterministic coverage/integrity audit for a single-venue CSV."""
    file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    rows: list[dict[str, str]] = []
    errors: list[str] = []
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, strict=True)
            missing = sorted(set(REQUIRED_FIELDS) - set(reader.fieldnames or []))
            if missing:
                errors.append(f"missing required columns: {', '.join(missing)}")
            else:
                rows = list(reader)
    except (csv.Error, UnicodeError) as exc:
        errors.append(f"CSV parse error: {exc}")

    timestamps: list[int] = []
    venues: set[str] = set()
    pairs: set[str] = set()
    invalid_rows = 0
    for line, row in enumerate(rows, start=2):
        try:
            if None in row:
                raise ValueError("row contains values beyond the declared CSV columns")
            empty_fields = [
                name
                for name in REQUIRED_FIELDS
                if not isinstance(row.get(name), str) or not row[name].strip()
            ]
            if empty_fields:
                raise ValueError(f"empty or missing required values: {', '.join(empty_fields)}")
            candle_timestamp = _utc_timestamp(row["timestamp"])
            vintage_timestamp = _utc_timestamp(row["vintage"])
            revision = int(row["revision"])
            if str(revision) != row["revision"].strip() or revision != 0:
                raise ValueError("revision must be exactly 0; revised candles are ambiguous")
            if vintage_timestamp > candle_timestamp:
                raise ValueError("vintage is later than candle close; not point-in-time eligible")
            timestamps.append(candle_timestamp)
            venues.add(row["venue"].strip())
            pairs.add(row["pair"].strip().upper())
            values = [float(row[name]) for name in ("open", "high", "low", "close", "volume")]
            opening, high, low, close, volume = values
            if (
                not all(math.isfinite(value) for value in values)
                or min(opening, high, low, close) <= 0
                or volume < 0
            ):
                raise ValueError("non-finite or non-positive OHLCV")
            if low > min(opening, close) or high < max(opening, close) or high < low:
                raise ValueError("impossible OHLC relationship")
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            invalid_rows += 1
            errors.append(f"line {line}: {exc}")

    ordered = sorted(timestamps)
    duplicates = len(ordered) - len(set(ordered))
    if duplicates:
        errors.append(f"{duplicates} duplicate hourly timestamps")
    unique = sorted(set(ordered))
    gaps = sum(
        max(0, (right - left) // HOUR_SECONDS - 1) for left, right in zip(unique, unique[1:])
    )
    if gaps:
        errors.append(f"{gaps} missing hourly candles")
    venue = next(iter(venues)) if len(venues) == 1 else None
    pair = next(iter(pairs)) if len(pairs) == 1 else None
    has_vintage = bool(rows and "vintage" in rows[0])
    has_revision = bool(rows and "revision" in rows[0])
    if len(venues) != 1:
        errors.append("dataset must contain exactly one venue")
    if len(pairs) != 1:
        errors.append("dataset must contain exactly one pair")
    if "" in venues:
        errors.append("venue identity must not be blank")
    if "" in pairs:
        errors.append("pair identity must not be blank")
    usd_pair = pair in {"BTC/USD", "XBT/USD", "BTCUSD", "XBTUSD"}
    if pair and not usd_pair:
        errors.append("non-USD pairs are not eligible; BTCUSDT is transfer-only")
    if rows and not has_vintage:
        errors.append("missing vintage provenance column")
    if rows and not has_revision:
        errors.append("missing revision provenance column")

    first = datetime.fromtimestamp(unique[0], timezone.utc) if unique else None
    last = datetime.fromtimestamp(unique[-1], timezone.utc) if unique else None
    span_hours = (unique[-1] - unique[0]) // HOUR_SECONDS + 1 if unique else 0
    target_timestamps = [value for value in unique if TARGET_START_TS <= value < TARGET_END_TS]
    warmup_timestamps = [value for value in unique if WARMUP_START_TS <= value < TARGET_START_TS]
    expected_target = (TARGET_END_TS - TARGET_START_TS) // HOUR_SECONDS
    expected_warmup = REQUIRED_WARMUP_DAYS * 24
    target_gaps = expected_target - len(target_timestamps)
    warmup_gaps = expected_warmup - len(warmup_timestamps)
    if target_gaps:
        errors.append(
            f"target period missing {target_gaps} of {expected_target} hourly observations"
        )
    if warmup_gaps:
        errors.append(f"warm-up missing {warmup_gaps} of {expected_warmup} hourly observations")
    eligible = (
        not errors and usd_pair and not invalid_rows and target_gaps == 0 and warmup_gaps == 0
    )
    if not unique:
        errors.append("no readable hourly observations")
    return {
        "status": "ready_for_replay" if eligible else "blocked",
        "eligible_for_skill_comparison": False,
        "reason": (
            "Dataset passes coverage and provenance gate; production-parity replay and validation "
            "are still required."
        )
        if eligible
        else "No eligible immutable same-venue BTC/USD corpus passes the minimum coverage and integrity gate.",
        "source_file": str(path),
        "source_sha256": file_hash,
        "venue": venue,
        "pair": pair,
        "row_count": len(rows),
        "unique_hourly_count": len(unique),
        "first_utc": first.isoformat() if first else None,
        "last_utc": last.isoformat() if last else None,
        "span_hours": span_hours,
        "target_period": {
            "start_inclusive": TARGET_START.isoformat(),
            "end_exclusive": TARGET_END.isoformat(),
            "expected_hours": expected_target,
            "observed_hours": len(target_timestamps),
            "missing_hours": target_gaps,
        },
        "target_period_observations": len(target_timestamps),
        "warmup_period": {
            "start_inclusive": datetime.fromtimestamp(WARMUP_START_TS, timezone.utc).isoformat(),
            "end_exclusive": TARGET_START.isoformat(),
            "expected_hours": expected_warmup,
            "observed_hours": len(warmup_timestamps),
            "missing_hours": warmup_gaps,
        },
        "warmup_days": round(len(warmup_timestamps) / 24, 3),
        "gaps": gaps,
        "duplicates": duplicates,
        "vintage_column_present": has_vintage,
        "revision_column_present": has_revision,
        "invalid_rows": invalid_rows,
        "errors": errors,
        "policy": "forecast_policy.PRODUCTION_POLICY; research ridge disabled",
        "research_models_enabled": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, help="single-venue hourly BTC/USD CSV")
    parser.add_argument("--output", type=Path, default=Path("canonical_benchmark_audit.json"))
    args = parser.parse_args()
    report = (
        audit_csv(args.data)
        if args.data
        else {
            "status": "blocked",
            "eligible_for_skill_comparison": False,
            "reason": "No canonical dataset supplied; Kraken OHLC is a limited recent window and Binance BTCUSDT is not BTC/USD.",
            "source_file": None,
            "source_sha256": None,
            "venue": None,
            "pair": None,
            "row_count": 0,
            "unique_hourly_count": 0,
            "first_utc": None,
            "last_utc": None,
            "span_hours": 0,
            "target_period": {
                "start_inclusive": TARGET_START.isoformat(),
                "end_exclusive": TARGET_END.isoformat(),
                "expected_hours": (TARGET_END_TS - TARGET_START_TS) // HOUR_SECONDS,
                "observed_hours": 0,
                "missing_hours": (TARGET_END_TS - TARGET_START_TS) // HOUR_SECONDS,
            },
            "target_period_observations": 0,
            "warmup_period": {
                "start_inclusive": datetime.fromtimestamp(
                    WARMUP_START_TS, timezone.utc
                ).isoformat(),
                "end_exclusive": TARGET_START.isoformat(),
                "expected_hours": REQUIRED_WARMUP_DAYS * 24,
                "observed_hours": 0,
                "missing_hours": REQUIRED_WARMUP_DAYS * 24,
            },
            "warmup_days": 0,
            "gaps": None,
            "duplicates": None,
            "vintage_column_present": False,
            "revision_column_present": False,
            "errors": ["eligible >=180-day same-venue BTC/USD corpus unavailable"],
            "policy": "forecast_policy.PRODUCTION_POLICY; research ridge disabled",
            "research_models_enabled": False,
        }
    )
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
