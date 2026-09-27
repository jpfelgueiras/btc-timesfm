"""Canonical v11 prospective scoring protocol integrity and pairing helpers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PROTOCOL_PATH = (
    Path(__file__).resolve().parents[3] / "docs/research/ISSUE_410_SCORING_PROTOCOL.json"
)


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize JSON deterministically as UTF-8, rejecting NaN/Infinity."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def protocol_digest(protocol: dict[str, Any]) -> str:
    """Return SHA-256 of the canonical protocol excluding its digest field."""
    unsigned = {key: value for key, value in protocol.items() if key != "protocol_sha256"}
    return hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()


def load_frozen_protocol(path: Path = PROTOCOL_PATH) -> dict[str, Any]:
    """Load and verify the committed protocol artifact; never score data."""
    protocol = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(protocol, dict) or protocol.get("schema_version") != 1:
        raise ValueError("unsupported protocol schema")
    claimed = protocol.get("protocol_sha256")
    if not isinstance(claimed, str) or claimed != protocol_digest(protocol):
        raise ValueError("protocol SHA-256 mismatch")
    return protocol


def parse_utc(value: str) -> datetime:
    """Parse a strict ISO timestamp and require an explicit UTC offset."""
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("timestamps must be UTC")
    return parsed.astimezone(timezone.utc)


def paired_attempts(
    rows: Iterable[dict[str, Any]], left: str, right: str
) -> list[tuple[dict, dict]]:
    """Return exact-key pairs only; reject duplicate keys and vintage mismatch."""
    indexed: dict[str, dict[tuple[datetime, datetime, int], dict[str, Any]]] = {left: {}, right: {}}
    for row in rows:
        model = row.get("model")
        if model not in indexed:
            continue
        origin = parse_utc(row["origin_at"])
        target = parse_utc(row["target_at"])
        horizon = row["horizon_hours"]
        if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon <= 0:
            raise ValueError("horizon_hours must be a positive integer")
        if target <= origin or int((target - origin).total_seconds()) != horizon * 3600:
            raise ValueError("target timestamp does not match origin and horizon")
        key = (origin, target, horizon)
        if key in indexed[model]:
            raise ValueError(f"duplicate attempt key for {model}")
        indexed[model][key] = row
    pairs = []
    for key in sorted(indexed[left].keys() & indexed[right].keys()):
        a, b = indexed[left][key], indexed[right][key]
        if a.get("vintage_id") != b.get("vintage_id"):
            raise ValueError("paired attempts must use identical vintage_id")
        if a.get("outcome") != b.get("outcome"):
            raise ValueError("paired attempts must use identical outcome")
        if a.get("status") == b.get("status") == "scored":
            pairs.append((a, b))
    return pairs


def validate_no_lookahead(row: dict[str, Any]) -> None:
    """Require forecast inputs and selection data to be available by origin."""
    origin = parse_utc(row["origin_at"])
    for field in ("input_available_at", "selection_data_through"):
        if field in row and parse_utc(row[field]) > origin:
            raise ValueError(f"{field} is after forecast origin")
