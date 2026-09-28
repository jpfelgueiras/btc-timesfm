#!/usr/bin/env python3
"""Export public, read-only JSON API snapshots for static GitHub Pages hosting."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from btc_timesfm.api.forecast_contract import API_VERSION, validate_forecast
from btc_timesfm.api.forecast_service import ForecastService, ServiceConfig
from btc_timesfm.history.history_store import DEFAULT_DB_PATH

STATIC_API_PAGE_SIZE = 100
PAGES_API_BASE_URL = "https://jpfelgueiras.github.io/btc-timesfm"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )


def generate_api(
    database: Path,
    output_dir: Path,
    *,
    now: datetime | None = None,
    page_size: int = STATIC_API_PAGE_SIZE,
) -> dict[str, Any]:
    """Export latest and paginated history snapshots from canonical SQLite history."""
    if not 1 <= page_size <= STATIC_API_PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {STATIC_API_PAGE_SIZE}")
    generated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    service = ForecastService(
        ServiceConfig(history_path=database, api_keys=frozenset({"static-export"})),
        clock=lambda: generated_at,
    )
    with service._connection() as connection:
        row = connection.execute("SELECT COUNT(*) FROM forecast_predictions").fetchone()
        record_count = int(row[0]) if row else 0
        forecasts = (
            service._query_rows(connection, "1 = 1", [], limit=record_count) if record_count else []
        )

    for index, forecast in enumerate(forecasts):
        errors = validate_forecast(forecast)
        if errors:
            raise ValueError(f"Forecast API record {index} violates the v1 contract: {errors}")

    snapshot_at = generated_at.isoformat().replace("+00:00", "Z")
    latest = forecasts[0] if forecasts else None
    latest_freshness = service._freshness(latest["origin_at"] if latest else None)
    latest_payload = {
        "api_version": API_VERSION,
        "snapshot_at": snapshot_at,
        "data": latest,
        "freshness": latest_freshness,
    }

    pages = [forecasts[index : index + page_size] for index in range(0, len(forecasts), page_size)]
    if not pages:
        pages = [[]]
    for page_number, records in enumerate(pages, start=1):
        if page_number == 1:
            relative_path = "api/v1/forecasts.json"
        else:
            relative_path = f"api/v1/forecasts/page-{page_number:04d}.json"
        next_url = None
        if page_number < len(pages):
            next_url = f"{PAGES_API_BASE_URL}/api/v1/forecasts/page-{page_number + 1:04d}.json"
        _write_json(
            output_dir / relative_path,
            {
                "api_version": API_VERSION,
                "snapshot_at": snapshot_at,
                "data": records,
                "freshness": latest_freshness,
                "pagination": {"page_size": page_size, "next_url": next_url},
            },
        )
    _write_json(output_dir / "api/v1/forecasts/latest.json", latest_payload)
    return {
        "api_version": API_VERSION,
        "snapshot_at": snapshot_at,
        "forecast_count": len(forecasts),
        "page_count": len(pages),
        "latest_origin_at": latest["origin_at"] if latest else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output-dir", type=Path, default=Path("build"))
    args = parser.parse_args()
    print(json.dumps(generate_api(args.db, args.output_dir), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
