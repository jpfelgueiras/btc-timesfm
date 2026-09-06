#!/usr/bin/env python3
"""Tests for structured forecast pipeline observability."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from btc_timesfm.ops.observability import PipelineObserver, run_stage, stable_run_id


class ObservabilityTests(unittest.TestCase):
    def make_observer(self, root: Path, *, run_id: str = "test-run") -> PipelineObserver:
        return PipelineObserver(
            report_path=root / "report.json",
            event_log_path=root / "events.jsonl",
            run_id=run_id,
        )

    def test_actions_run_id_is_stable_across_processes(self) -> None:
        with patch.dict(
            os.environ,
            {"GITHUB_RUN_ID": "12345", "GITHUB_RUN_ATTEMPT": "2"},
            clear=False,
        ):
            self.assertEqual(stable_run_id(), "github-12345-2")

    def test_stage_records_timing_and_jsonl_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observer = self.make_observer(root)
            with observer.stage("model_inference", model="timesfm"):
                pass
            observer.finalize("success")

            report = json.loads((root / "report.json").read_text())
            self.assertEqual(report["status"], "success")
            self.assertEqual(report["stages"][0]["name"], "model_inference")
            self.assertEqual(report["stages"][0]["status"], "success")
            self.assertGreaterEqual(report["stages"][0]["duration_ms"], 0.0)
            events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
            self.assertTrue(all(event["run_id"] == "test-run" for event in events))
            self.assertIn("stage_started", {event["event"] for event in events})
            self.assertIn("stage_finished", {event["event"] for event in events})

    def test_stage_failure_is_persisted_and_counted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            observer = self.make_observer(Path(directory))
            with self.assertRaisesRegex(RuntimeError, "boom"):
                with observer.stage("history_persistence"):
                    raise RuntimeError("boom")

            self.assertEqual(observer.data["counters"]["failures"], 1)
            stage = observer.data["stages"][-1]
            self.assertEqual(stage["status"], "failed")
            self.assertEqual(stage["error_type"], "RuntimeError")

    def test_reload_preserves_counters_and_links_experiment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.make_observer(root)
            first.increment("fallbacks")

            second = self.make_observer(root)
            second.set_experiment_id("production_forecast-abc")

            self.assertEqual(second.data["counters"]["fallbacks"], 1)
            self.assertEqual(second.data["experiment_id"], "production_forecast-abc")

    def test_skip_is_terminal_and_visible_in_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            observer = self.make_observer(Path(directory))
            observer.skip("completed candle gap is below two hours")
            observer.finalize("success", preserve_terminal=True)

            self.assertEqual(observer.data["status"], "skipped")
            self.assertEqual(observer.data["counters"]["skips"], 1)
            summary = observer.summary_markdown()
            self.assertIn("Status: **skipped**", summary)
            self.assertIn("completed candle gap", summary)

    def test_run_stage_counts_success_and_propagates_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            observer = self.make_observer(Path(directory))
            ok = run_stage(
                observer,
                "x_post",
                [sys.executable, "-c", "raise SystemExit(0)"],
                "successful_posts",
            )
            failed = run_stage(
                observer,
                "x_post",
                [sys.executable, "-c", "raise SystemExit(7)"],
                "successful_posts",
            )

            self.assertEqual(ok, 0)
            self.assertEqual(failed, 7)
            self.assertEqual(observer.data["counters"]["successful_posts"], 1)
            self.assertEqual(observer.data["counters"]["failures"], 1)


if __name__ == "__main__":
    unittest.main()
