"""Deterministic vertical-path coverage for public forecast data."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np

from btc_timesfm.api.forecast_contract import (
    validate_historical_response,
    validate_latest_response,
)
from btc_timesfm.api.forecast_service import ForecastService, ServiceConfig
from btc_timesfm.cli import btc_forecast
from btc_timesfm.data.market_data_sources import (
    NoHealthyMarketDataProvider,
    ProviderConfig,
    select_market_data,
)
from btc_timesfm.data.market_data_validation import ValidationConfig
from btc_timesfm.forecasting.forecast_engine import MarketData
from btc_timesfm.history.history_store import ForecastHistoryStore
from btc_timesfm.ops import observability, validated_entrypoints
from btc_timesfm.ops.validated_entrypoints import run_forecast
from btc_timesfm.web.static_site import build_site_data


class _Provider:
    name = "fixture"
    pair = "BTC/USD"

    def __init__(self, data: MarketData) -> None:
        self.data = data

    def fetch(self, limit: int) -> MarketData:
        return self.data


class VerticalForecastPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.origin = int(
            datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0).timestamp()
        )
        self.data = self._candles()
        self.database = self.root / "history.sqlite"
        self.output = self.root / "forecast.json"
        self.state = self.root / "state.json"
        self.health = self.root / "health.json"
        self.audit = self.root / "audit.jsonl"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _candles(self) -> MarketData:
        timestamps = list(range(self.origin - 511 * 3600, self.origin + 1, 3600))
        close = np.asarray(
            [60_000.0 + i * 0.5 for i in range(len(timestamps))], dtype=np.float64
        )
        return MarketData(
            timestamps=timestamps,
            opens=close - 1,
            highs=close + 5,
            lows=close - 5,
            closes=close,
            volumes=np.full(len(timestamps), 10.0),
        )

    def _engine_result(
        self, _model: object, data: MarketData, *args: object, **kwargs: object
    ) -> dict[str, object]:
        close = float(data.closes[-1])
        predictions = {
            f"{hour}h": {
                "price_usd": close + hour,
                "change_pct": hour / close * 100.0,
                "q10_usd": close - 2 * hour,
                "q50_usd": close + hour,
                "q90_usd": close + 3 * hour,
                "model_agreement": 0.75,
            }
            for hour in (2, 4, 8, 16)
        }
        model_predictions = {
            "timesfm_168h": {horizon: dict(item) for horizon, item in predictions.items()}
        }
        return {
            "latest_close_at": datetime.fromtimestamp(
                data.timestamps[-1], timezone.utc
            ).isoformat(),
            "latest_close_usd": close,
            "regime": "range",
            "market_features": {},
            "model_weights": {horizon: {"timesfm_168h": 1.0} for horizon in predictions},
            "model_predictions": model_predictions,
            "predictions": predictions,
            "weighting_diagnostics": {},
        }

    def _run(
        self,
        *,
        fetch_error: Exception | None = None,
        inference_error: Exception | None = None,
        persistence_error: bool = False,
    ) -> None:
        selection = None
        if fetch_error is None:
            selection = select_market_data(
                primary_provider=_Provider(self.data),
                secondary_provider=_Provider(self.data),
                now=datetime.fromtimestamp(self.origin + 60, timezone.utc),
                validation_config=ValidationConfig(),
                provider_config=ProviderConfig(),
            )
        original_store = btc_forecast.ForecastHistoryStore

        class FailingStore(original_store):
            def ingest_snapshot(inner_self, *args: object, **kwargs: object):
                if persistence_error:
                    raise OSError("fixture persistence failure")
                return super().ingest_snapshot(*args, **kwargs)

        patches = [
            (
                patch.object(
                    btc_forecast, "fetch_redundant_hourly", side_effect=fetch_error
                )
                if fetch_error is not None
                else patch.object(
                    btc_forecast, "fetch_redundant_hourly", return_value=selection
                )
            ),
            patch.object(btc_forecast, "load_timesfm", return_value=object()),
            patch.object(
                btc_forecast,
                "build_forecast",
                side_effect=inference_error or self._engine_result,
            ),
            patch.object(
                btc_forecast,
                "evaluate_production_drift",
                return_value={
                    "severity": "none",
                    "adaptive_confidence": 1.0,
                    "configuration": {},
                    "summary": {},
                },
            ),
            patch.object(
                btc_forecast,
                "fetch_derivatives_snapshot",
                return_value={"status": "unavailable", "features": {}},
            ),
            patch.object(
                btc_forecast,
                "fetch_microstructure_snapshot",
                return_value={"status": "unavailable", "features": {}},
            ),
            patch.object(
                btc_forecast,
                "fetch_cross_asset_snapshot",
                return_value={"status": "unavailable", "features": {}},
            ),
            patch.object(
                btc_forecast,
                "evaluate_source_health",
                return_value={"quarantined_sources": [], "metrics": {}},
            ),
            patch.object(
                btc_forecast, "retain_optional_sources", return_value={"status": "retained"}
            ),
            patch.object(btc_forecast, "ForecastHistoryStore", FailingStore),
            patch.object(btc_forecast, "HISTORY_DB_PATH", self.database),
            patch.object(btc_forecast, "OUTPUT_PATH", self.output),
            patch.object(btc_forecast, "STATE_PATH", self.state),
            patch.object(btc_forecast, "TWEET_PATH", self.root / "tweet.txt"),
            patch.object(btc_forecast, "DERIVATIVES_PATH", self.root / "derivatives.json"),
            patch.object(btc_forecast, "MICROSTRUCTURE_PATH", self.root / "microstructure.json"),
            patch.object(btc_forecast, "CROSS_ASSET_PATH", self.root / "cross_asset.json"),
            patch.object(btc_forecast, "SHADOW_DB_PATH", self.root / "shadow.sqlite"),
            patch.object(btc_forecast, "SHADOW_REPORT_PATH", self.root / "shadow.json"),
            patch.object(btc_forecast, "SHADOW_SUMMARY_PATH", self.root / "shadow.md"),
            patch.object(observability, "REPORT_PATH", self.root / "observability.json"),
            patch.object(observability, "EVENT_LOG_PATH", self.root / "observability.jsonl"),
            patch.object(
                validated_entrypoints,
                "PipelineObserver",
                side_effect=lambda **kwargs: observability.PipelineObserver(
                    report_path=self.root / "observability.json",
                    event_log_path=self.root / "observability.jsonl",
                    **kwargs,
                ),
            ),
            patch.dict("os.environ", {"BTC_RUN_ID": "vertical-run-336"}),
        ]
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            run_forecast([])

    def test_validated_candles_flow_to_sqlite_api_and_site(self) -> None:
        self._run()

        self.assertTrue(self.output.exists())
        output = json.loads(self.output.read_text(encoding="utf-8"))
        manifest = output["experiment_manifest"]
        self.assertEqual(
            output["latest_close_at"],
            datetime.fromtimestamp(self.origin, timezone.utc).isoformat(),
        )
        self.assertEqual(manifest["run_type"], "production_forecast")
        self.assertEqual(manifest["configuration"]["policy"]["name"], "production")

        store = ForecastHistoryStore(self.database)
        row = next(
            item
            for item in store.export_rows()
            if item["model_name"] == "ensemble" and item["horizon_hours"] == 4
        )
        self.assertEqual(
            row["target_at"],
            datetime.fromtimestamp(self.origin + 4 * 3600, timezone.utc).isoformat(),
        )
        self.assertEqual(row["experiment_run_id"], manifest["run_id"])
        self.assertEqual(row["configuration_id"], manifest["configuration_id"])

        health_payload = {
            "overall_health": "healthy",
            "stages": {
                stage: {"health": "healthy", "circuit_state": "closed"}
                for stage in ("market_data", "forecast", "history", "x_post")
            },
        }
        self.health.write_text(json.dumps(health_payload), encoding="utf-8")
        service = ForecastService(
            ServiceConfig(self.database, frozenset({"test-key"}), self.health, self.audit),
            clock=lambda: datetime.fromtimestamp(self.origin + 600, timezone.utc),
        )
        captured: dict[str, object] = {}
        body = b"".join(
            service(
                {
                    "REQUEST_METHOD": "GET",
                    "PATH_INFO": "/v1/forecasts/latest",
                    "HTTP_AUTHORIZATION": "Bearer test-key",
                    "wsgi.input": io.BytesIO(),
                },
                lambda status, headers: captured.update(status=status, headers=headers),
            )
        )
        api = json.loads(body)
        self.assertTrue(str(captured["status"]).startswith("200"))
        self.assertEqual(validate_latest_response(api), [])
        latest = api["data"]
        self.assertEqual(latest["origin_at"], row["origin_at"])
        self.assertEqual(latest["freshness"]["status"], "fresh")
        captured.clear()
        history_body = b"".join(
            service(
                {
                    "REQUEST_METHOD": "GET",
                    "PATH_INFO": "/v1/forecasts",
                    "QUERY_STRING": "model=ensemble&horizon_hours=4",
                    "HTTP_AUTHORIZATION": "Bearer test-key",
                    "wsgi.input": io.BytesIO(),
                },
                lambda status, headers: captured.update(status=status, headers=headers),
            )
        )
        historical = json.loads(history_body)
        self.assertEqual(validate_historical_response(historical), [])
        forecast = historical["data"][0]
        self.assertEqual(forecast["origin_at"], row["origin_at"])
        self.assertEqual(forecast["target_at"], row["target_at"])
        self.assertEqual(forecast["run"]["id"], manifest["run_id"])
        self.assertEqual(forecast["model"]["configuration_id"], manifest["configuration_id"])
        self.assertEqual(forecast["forecast_id"], f"{row['origin_at']}:ensemble:4")
        self.assertEqual(forecast["freshness"]["status"], "fresh")

        site = build_site_data(
            store.export_rows(),
            now=datetime.fromtimestamp(self.origin + 600, timezone.utc),
            latest_snapshot=output,
        )
        site_latest = next(
            item for item in site["latest"]["predictions"] if item["horizon_hours"] == 4
        )
        self.assertEqual(site["latest"]["origin_at"], forecast["origin_at"])
        self.assertEqual(site_latest["target_at"], forecast["target_at"])
        self.assertEqual(site["latest_age_hours"], 0.17)

    def test_stale_and_malformed_provider_errors_never_publish_forecasts(self) -> None:
        cases = (
            ("stale", "stale_data"),
            ("malformed", "invalid_ohlcv"),
        )
        for input_name, failure_class in cases:
            with self.subTest(input=input_name):
                (self.root / "observability.json").unlink(missing_ok=True)
                (self.root / "observability.jsonl").unlink(missing_ok=True)
                with self.assertRaises(NoHealthyMarketDataProvider):
                    self._run(
                        fetch_error=NoHealthyMarketDataProvider(
                            f"fixture provider rejected {input_name} candles: {failure_class}"
                        )
                    )
                self.assertFalse(self.database.exists())
                self.assertFalse(self.output.exists())
                report = json.loads(
                    (self.root / "observability.json").read_text(encoding="utf-8")
                )
                stage = next(
                    item for item in report["stages"] if item["name"] == "market_data_fetch"
                )
                self.assertEqual(stage["status"], "failed")
                self.assertEqual(stage["error_type"], "NoHealthyMarketDataProvider")

    def test_inference_and_persistence_failures_have_failed_stages_and_no_publication(
        self,
    ) -> None:
        cases = (
            (
                "inference",
                RuntimeError("fixture inference failure"),
                False,
                "model_inference",
                "RuntimeError",
            ),
            ("persistence", None, True, "history_persistence", "OSError"),
        )
        for name, inference_error, persistence_error, failed_stage, failure_class in cases:
            with self.subTest(failure=name):
                with self.assertRaises((RuntimeError, OSError)):
                    self._run(
                        inference_error=inference_error,
                        persistence_error=persistence_error,
                    )
                self.assertFalse(self.output.exists())
                if self.database.exists():
                    self.assertEqual(ForecastHistoryStore(self.database).stats()["origins"], 0)
                report = json.loads((self.root / "observability.json").read_text(encoding="utf-8"))
                stage = next(item for item in report["stages"] if item["name"] == failed_stage)
                self.assertEqual(stage["status"], "failed")
                self.assertEqual(stage["error_type"], failure_class)


if __name__ == "__main__":
    unittest.main()
