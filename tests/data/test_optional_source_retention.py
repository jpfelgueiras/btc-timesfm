"""Tests for bounded, immutable optional-source retention and replay."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from btc_timesfm.data.optional_source_retention import (
    OptionalSourceRetentionConfig,
    replay_optional_sources,
    retain_optional_sources,
)


NOW = datetime(2026, 9, 5, 18, tzinfo=timezone.utc)


class OptionalSourceRetentionTests(unittest.TestCase):
    def test_retention_is_bounded_and_replay_does_not_mutate_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "retention.json"
            config = OptionalSourceRetentionConfig(retention_hours=2, max_records=2)
            observed = NOW + timedelta(hours=3)
            for offset in range(3):
                retain_optional_sources(
                    {"derivatives": {"features": {"signal": offset}}},
                    origin_at=NOW + timedelta(hours=offset),
                    observed_at=observed,
                    metadata_per_source={"derivatives": {"status": "ok", "available": True}},
                    path=path,
                    config=config,
                )
            persisted = path.read_text(encoding="utf-8")
            payload = json.loads(persisted)
            self.assertEqual(len(payload["records"]), 2)

            replay = replay_optional_sources(NOW + timedelta(hours=2), path=path)
            replay["snapshots"]["derivatives"]["data"]["features"]["signal"] = 99
            self.assertEqual(path.read_text(encoding="utf-8"), persisted)
            self.assertEqual(replay["features"]["derivatives"], {"signal": 2})

    def test_replay_can_reconstruct_features_from_retained_raw_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "retention.json"
            retain_optional_sources(
                {"microstructure": {"raw": {"bid": 100, "ask": 102}, "features": {}}},
                origin_at=NOW,
                metadata_per_source={"microstructure": {"status": "ok", "available": True}},
                path=path,
            )
            replay = replay_optional_sources(
                NOW,
                path=path,
                reconstruct=lambda _source, snapshot: {
                    "spread": snapshot["raw"]["ask"] - snapshot["raw"]["bid"]
                },
            )
            self.assertTrue(replay["replayed"])
            self.assertEqual(replay["features"]["microstructure"], {"spread": 2})
            # Check that metadata is returned in replay
            self.assertIn("metadata", replay)
            self.assertIn("microstructure", replay["metadata"])
            self.assertEqual(replay["metadata"]["microstructure"]["status"], "ok")
            self.assertEqual(replay["metadata"]["microstructure"]["available"], True)


if __name__ == "__main__":
    unittest.main()
