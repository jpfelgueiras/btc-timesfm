"""Tests for external optional-source health monitoring."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from btc_timesfm.data.source_health import SourceHealthConfig, evaluate_source_health


NOW = datetime(2026, 9, 5, 18, tzinfo=timezone.utc)


def snapshot(
    *, origin: datetime = NOW, features: dict[str, float] | None = None
) -> dict[str, object]:
    return {
        "origin_at": origin.isoformat(),
        "status": "ok",
        "features": features if features is not None else {"signal": 1.0},
        "quality": {"missing_features": []},
    }


class SourceHealthTests(unittest.TestCase):
    def health(self, sources: dict[str, dict[str, object]], state: Path) -> dict[str, object]:
        return evaluate_source_health(
            sources,
            origin_at=NOW,
            config=SourceHealthConfig(max_optional_age_hours=2.5, max_missing_feature_ratio=0.25),
            state_path=state,
        )

    def test_stale_optional_source_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = self.health(
                {"derivatives": snapshot(origin=NOW - timedelta(hours=3))},
                Path(directory) / "state.json",
            )
            source = report["sources"]["derivatives"]
            self.assertIn("derivatives", report["quarantined_sources"])
            self.assertEqual(source["quarantine_reasons"], ["stale"])

    def test_incomplete_optional_source_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = snapshot(features={"signal": 1.0})
            item["quality"] = {"missing_features": ["a", "b"]}
            report = self.health({"cross_asset": item}, Path(directory) / "state.json")
            source = report["sources"]["cross_asset"]
            self.assertEqual(source["missing_feature_ratio"], 0.666667)
            self.assertIn("incomplete", source["quarantine_reasons"])

    def test_revised_source_is_quarantined_after_first_observation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            self.health({"microstructure": snapshot(features={"signal": 1.0})}, state)
            report = self.health({"microstructure": snapshot(features={"signal": 2.0})}, state)
            source = report["sources"]["microstructure"]
            self.assertTrue(source["revised"])
            self.assertIn("revised", source["quarantine_reasons"])
            self.assertTrue(json.loads(state.read_text()))

    def test_provider_disagreement_is_reported_and_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = evaluate_source_health(
                {"derivatives": snapshot()},
                origin_at=NOW,
                market_comparison={"status": "disagreement", "max_close_difference_pct": 2.0},
                state_path=Path(directory) / "state.json",
            )
            self.assertIn("market_data", report["quarantined_sources"])
            self.assertEqual(report["metrics"]["disagreement_count"], 1)


if __name__ == "__main__":
    unittest.main()
