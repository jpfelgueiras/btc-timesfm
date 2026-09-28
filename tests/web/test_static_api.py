from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from btc_timesfm.api.forecast_contract import validate_forecast
from btc_timesfm.history.history_store import ForecastHistoryStore
from btc_timesfm.web.static_api import generate_api


class StaticApiTests(unittest.TestCase):
    def test_exports_latest_and_full_history_contract_resources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "history.sqlite"
            store = ForecastHistoryStore(database)
            for origin in ("2026-09-16T11:00:00Z", "2026-09-16T12:00:00Z"):
                store.ingest_snapshot(
                    {
                        "generated_at": origin,
                        "latest_close_at": origin,
                        "latest_close_usd": 60000.0,
                        "source": "kraken",
                        "pair": "BTC/USD",
                        "predictions": {
                            "4h": {
                                "price_usd": 61000.0,
                                "change_pct": 1.0,
                                "q10_usd": 59000.0,
                                "q50_usd": 61000.0,
                                "q90_usd": 63000.0,
                                "model_agreement": 0.75,
                            }
                        },
                    }
                )

            built_at = datetime(2026, 9, 16, 13, tzinfo=timezone.utc)
            summary = generate_api(database, root / "site", now=built_at)
            latest = json.loads(
                (root / "site/api/v1/forecasts/latest.json").read_text(encoding="utf-8")
            )
            history = json.loads((root / "site/api/v1/forecasts.json").read_text(encoding="utf-8"))

        self.assertEqual(summary["forecast_count"], 2)
        self.assertEqual(summary["snapshot_at"], "2026-09-16T13:00:00Z")
        self.assertEqual(latest["data"]["origin_at"], "2026-09-16T12:00:00+00:00")
        self.assertEqual(len(history["data"]), 2)
        self.assertEqual(history["data"][0], latest["data"])
        self.assertEqual(history["pagination"], {"page_size": 100, "next_url": None})
        self.assertEqual(validate_forecast(latest["data"]), [])
        self.assertEqual(latest["freshness"]["observed_at"], latest["data"]["origin_at"])

    def test_history_snapshot_pages_expose_a_next_url(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "history.sqlite"
            store = ForecastHistoryStore(database)
            for hour in range(3):
                origin = f"2026-09-16T{hour + 10:02d}:00:00Z"
                store.ingest_snapshot(
                    {
                        "generated_at": origin,
                        "latest_close_at": origin,
                        "latest_close_usd": 60000.0,
                        "source": "kraken",
                        "pair": "BTC/USD",
                        "predictions": {
                            "4h": {
                                "price_usd": 61000.0,
                                "change_pct": 1.0,
                                "q10_usd": 59000.0,
                                "q50_usd": 61000.0,
                                "q90_usd": 63000.0,
                            }
                        },
                    }
                )

            summary = generate_api(
                database,
                root / "site",
                now=datetime(2026, 9, 16, 13, tzinfo=timezone.utc),
                page_size=2,
            )
            first = json.loads((root / "site/api/v1/forecasts.json").read_text(encoding="utf-8"))
            second = json.loads(
                (root / "site/api/v1/forecasts/page-0002.json").read_text(encoding="utf-8")
            )

        self.assertEqual(summary["page_count"], 2)
        self.assertEqual(len(first["data"]), 2)
        self.assertEqual(len(second["data"]), 1)
        self.assertEqual(
            first["pagination"]["next_url"],
            "https://jpfelgueiras.github.io/btc-timesfm/api/v1/forecasts/page-0002.json",
        )
        self.assertIsNone(second["pagination"]["next_url"])
        paged_origins = [item["origin_at"] for item in first["data"] + second["data"]]
        self.assertEqual(paged_origins, sorted(paged_origins, reverse=True))

    def test_empty_history_emits_a_valid_empty_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "empty.sqlite"
            ForecastHistoryStore(database)
            summary = generate_api(
                database,
                root / "site",
                now=datetime(2026, 9, 16, 13, tzinfo=timezone.utc),
            )
            latest = json.loads(
                (root / "site/api/v1/forecasts/latest.json").read_text(encoding="utf-8")
            )
            history = json.loads((root / "site/api/v1/forecasts.json").read_text(encoding="utf-8"))

        self.assertEqual(summary["forecast_count"], 0)
        self.assertIsNone(latest["data"])
        self.assertEqual(history["data"], [])
        self.assertEqual(latest["freshness"]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
