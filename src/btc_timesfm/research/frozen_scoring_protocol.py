"""Canonical v11 prospective scoring protocol integrity and pairing helpers."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PROTOCOL_PATH = (
    Path(__file__).resolve().parents[3] / "docs/research/ISSUE_410_SCORING_PROTOCOL.json"
)
SCHEMA_PATH = PROTOCOL_PATH.with_name("ISSUE_410_SCORING_PROTOCOL.schema.json")


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
    """Load, schema-validate, and hash-verify the artifact; never score data."""
    protocol = json.loads(path.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        import jsonschema

        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(protocol, schema)
    except ImportError as exc:
        raise RuntimeError("jsonschema is required to validate the frozen protocol") from exc
    except jsonschema.exceptions.SchemaError as exc:
        raise ValueError("invalid protocol JSON Schema") from exc
    except jsonschema.exceptions.ValidationError as exc:
        raise ValueError(f"protocol schema validation failed: {exc.message}") from exc
    claimed = protocol.get("protocol_sha256")
    if not isinstance(claimed, str) or claimed != protocol_digest(protocol):
        raise ValueError("protocol SHA-256 mismatch")
    return protocol


def parse_utc(value: str) -> datetime:
    """Parse a strict ISO timestamp and require an explicit UTC offset."""
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    match = re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(\d+))?(?:Z|\+00:00)", value)
    if match is None or (match.group(1) is not None and len(match.group(1)) > 6):
        raise ValueError("timestamp must be strict ISO-8601 UTC with at most microsecond precision")
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
        if target <= origin or (target - origin).total_seconds() != horizon * 3600:
            raise ValueError("target timestamp does not match origin and horizon")
        key = (origin, target, horizon)
        if key in indexed[model]:
            raise ValueError(f"duplicate attempt key for {model}")
        vintage_id = row.get("vintage_id")
        vintage_sha256 = row.get("vintage_sha256")
        if not isinstance(vintage_id, str) or not vintage_id:
            raise ValueError("missing required provenance field: vintage_id")
        if (
            not isinstance(vintage_sha256, str)
            or len(vintage_sha256) != 64
            or any(char not in "0123456789abcdef" for char in vintage_sha256)
        ):
            raise ValueError("vintage_sha256 must be a lowercase SHA-256 digest")
        indexed[model][key] = row
    pairs = []
    for key in sorted(indexed[left].keys() & indexed[right].keys()):
        a, b = indexed[left][key], indexed[right][key]
        if a.get("vintage_id") != b.get("vintage_id"):
            raise ValueError("paired attempts must use identical vintage_id")
        if a.get("vintage_sha256") != b.get("vintage_sha256"):
            raise ValueError("paired attempts must use identical vintage_sha256")
        if a.get("outcome") != b.get("outcome"):
            raise ValueError("paired attempts must use identical outcome")
        if a.get("status") == b.get("status") == "scored":
            pairs.append((a, b))
    return pairs


def validate_no_lookahead(row: dict[str, Any]) -> None:
    """Require input-vintage and selection-cutoff provenance at/before origin."""
    vintage_id = row.get("vintage_id")
    vintage_sha256 = row.get("vintage_sha256")
    if not isinstance(vintage_id, str) or not vintage_id:
        raise ValueError("missing required provenance field: vintage_id")
    if (
        not isinstance(vintage_sha256, str)
        or len(vintage_sha256) != 64
        or any(char not in "0123456789abcdef" for char in vintage_sha256)
    ):
        raise ValueError("missing or invalid required provenance field: vintage_sha256")
    origin = parse_utc(row["origin_at"])
    for field in ("input_available_at", "selection_data_through"):
        if field not in row or row[field] is None:
            raise ValueError(f"missing required provenance field: {field}")
        if parse_utc(row[field]) > origin:
            raise ValueError(f"{field} is after forecast origin")
