#!/usr/bin/env python3
"""Validate generated site output against a strict contract before deploy."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

REQUIRED_DATA_KEYS: dict[str, type | tuple[type, ...]] = {
    "generated_at": str,
    "latest": (dict, type(None)),
    "latest_age_hours": (float, int, type(None)),
    "accuracy": dict,
    "persistence_edge": dict,
    "recent": list,
    "matured_rows": int,
    "horizons": list,
    "low_sample_threshold": int,
    "multi_horizon_coherence": (dict, type(None)),
    "database_verification": dict,
    "schema_version": int,
}

REQUIRED_HTML_SECTIONS = [
    "Forecasts & accuracy",
    "Accuracy",
    "Ensemble edge vs persistence",
    "Forecast History Explorer",
]

PLACEHOLDER_MARKERS = re.compile(r"\b(TODO|FIXME|PLACEHOLDER|LOREM IPSUM)\b|\b(undefined|null)\b")
ERROR_MARKERS = re.compile(r"\bERROR\b")


class _HTMLSectionChecker(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.text_chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self.text_chunks.append(data)


def _check_html_well_formed(html_text: str) -> list[str]:
    errors: list[str] = []
    if not html_text.strip():
        errors.append("HTML file is empty")
        return errors
    if "<!doctype html>" not in html_text.lower() and "<html" not in html_text.lower():
        errors.append("Missing doctype or <html> tag")
    if "</html>" not in html_text.lower():
        errors.append("Missing closing </html> tag")
    for tag in ("main", "head", "body"):
        if f"<{tag}" not in html_text.lower() or f"</{tag}>" not in html_text.lower():
            errors.append(f"Missing <{tag}> or </{tag}>")
    return errors


def _check_html_sections(html_text: str) -> list[str]:
    errors: list[str] = []
    for section in REQUIRED_HTML_SECTIONS:
        if section not in html_text:
            errors.append(f"Required HTML section missing: {section}")
    return errors


def _check_html_placeholders(html_text: str) -> list[str]:
    errors: list[str] = []
    placeholder_matches = PLACEHOLDER_MARKERS.findall(html_text)
    if placeholder_matches:
        errors.append(f"Placeholder markers found in HTML: {placeholder_matches}")
    error_matches = ERROR_MARKERS.findall(html_text)
    if error_matches:
        errors.append(f"ERROR markers found in HTML: {error_matches}")
    return errors


def _validate_iso_timestamp(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return False
        return True
    except (ValueError, TypeError):
        return False


def _has_placeholders(obj: Any, path: str = "") -> list[str]:
    errors: list[str] = []
    if isinstance(obj, str):
        if PLACEHOLDER_MARKERS.search(obj):
            errors.append(f"Placeholder/error marker found at {path or '<root>'}: {obj!r}")
        if ERROR_MARKERS.search(obj):
            errors.append(f"ERROR marker found at {path or '<root>'}: {obj!r}")
    elif isinstance(obj, dict):
        for key, value in obj.items():
            errors.extend(_has_placeholders(value, f"{path}.{key}" if path else str(key)))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            errors.extend(_has_placeholders(item, f"{path}[{i}]"))
    return errors


def validate_data_json(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    for key, expected_type in REQUIRED_DATA_KEYS.items():
        if key not in data:
            errors.append(f"Missing required key: {key}")
            continue
        if not isinstance(data[key], expected_type):
            errors.append(
                f"Wrong type for {key}: expected {expected_type}, got {type(data[key]).__name__}"
            )

    if "schema_version" in data:
        if data["schema_version"] != SCHEMA_VERSION:
            errors.append(
                f"schema_version mismatch: expected {SCHEMA_VERSION}, got {data['schema_version']}"
            )

    if "generated_at" in data and isinstance(data["generated_at"], str):
        if not _validate_iso_timestamp(data["generated_at"]):
            errors.append(f"Invalid generated_at timestamp: {data['generated_at']!r}")

    if "latest_age_hours" in data and isinstance(data["latest_age_hours"], (int, float)):
        if data["latest_age_hours"] < 0:
            errors.append(f"latest_age_hours is negative: {data['latest_age_hours']}")
        if data["latest_age_hours"] > 168:
            errors.append(f"latest_age_hours is unreasonably large: {data['latest_age_hours']}")

    if "matured_rows" in data and isinstance(data["matured_rows"], int):
        if data["matured_rows"] < 0:
            errors.append(f"matured_rows is negative: {data['matured_rows']}")

    errors.extend(_has_placeholders(data))
    return errors


def validate_site_output(output_dir: Path) -> dict[str, Any]:
    errors: list[str] = []
    data: dict[str, Any] = {}
    html_path = output_dir / "index.html"
    data_path = output_dir / "data.json"

    if not html_path.exists():
        errors.append(f"Missing {html_path.name}")
    else:
        html_text = html_path.read_text(encoding="utf-8")
        errors.extend(_check_html_well_formed(html_text))
        errors.extend(_check_html_sections(html_text))
        errors.extend(_check_html_placeholders(html_text))

    if not data_path.exists():
        errors.append(f"Missing {data_path.name}")
    else:
        try:
            data = json.loads(data_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"Failed to parse data.json: {exc}")
            data = {}
        if isinstance(data, dict):
            errors.extend(validate_data_json(data))

    schema_version = data.get("schema_version") if isinstance(data, dict) else None
    return {
        "passed": len(errors) == 0,
        "errors": errors,
        "schema_version": schema_version,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate site output contract")
    parser.add_argument("output_dir", type=Path, help="Site output directory")
    args = parser.parse_args()

    result = validate_site_output(args.output_dir)
    print(json.dumps(result, indent=2))
    if not result["passed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
