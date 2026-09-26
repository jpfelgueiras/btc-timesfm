from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from btc_timesfm.ops.release_state import (
    ReleaseStateError,
    persist_assets,
    validate_restored_state,
)


class ReleaseStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.asset = self.root / "x_post_registry.json"
        self.asset.write_text(json.dumps({"version": 1, "posts": {}}), encoding="utf-8")
        self.commands: list[list[str]] = []
        self.fail_uploads = 0
        self.corrupt_readback = False

    def tearDown(self) -> None:
        self.temp.cleanup()

    def gh(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command[2] == "upload" and self.fail_uploads:
            self.fail_uploads -= 1
            raise subprocess.CalledProcessError(1, command)
        if command[2] == "download":
            target = Path(command[command.index("--dir") + 1]) / self.asset.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"corrupt" if self.corrupt_readback else self.asset.read_bytes())
        return subprocess.CompletedProcess(command, 0, "", "")

    def test_transient_upload_failure_retries_and_verifies_readback(self) -> None:
        self.fail_uploads = 1
        sleep = Mock()
        persist_assets(
            [self.asset], release_tag="history", repository="owner/repo", run_id="42",
            run=self.gh, sleep=sleep,
        )
        self.assertEqual(sum(command[2] == "upload" for command in self.commands), 2)
        self.assertEqual(sum(command[2] == "download" for command in self.commands), 1)
        sleep.assert_called_once_with(1)

    def test_retry_exhaustion_fails_without_logging_command_output(self) -> None:
        self.fail_uploads = 3
        with self.assertRaisesRegex(ReleaseStateError, "run_id=42.*attempts=3"):
            persist_assets(
                [self.asset], release_tag="history", repository="owner/repo", run_id="42",
                attempts=3, run=self.gh, sleep=Mock(),
            )
        self.assertEqual(sum(command[2] == "upload" for command in self.commands), 3)

    def test_digest_mismatch_retries_then_fails(self) -> None:
        self.corrupt_readback = True
        with self.assertRaisesRegex(ReleaseStateError, "digest_mismatch"):
            persist_assets(
                [self.asset], release_tag="history", repository="owner/repo", run_id="42",
                attempts=2, run=self.gh, sleep=Mock(),
            )
        self.assertEqual(sum(command[2] == "download" for command in self.commands), 2)

    def test_missing_release_is_first_run_but_missing_state_in_existing_release_fails(self) -> None:
        validate_restored_state([self.root / "pipeline_health.json"], release_exists=False)
        with self.assertRaisesRegex(ReleaseStateError, "missing required state"):
            validate_restored_state([self.root / "pipeline_health.json"], release_exists=True)

    def test_corrupt_restored_state_fails_and_valid_state_recovers_next_run(self) -> None:
        path = self.root / "pipeline_health.json"
        path.write_text("not json", encoding="utf-8")
        with self.assertRaisesRegex(ReleaseStateError, "corrupt"):
            validate_restored_state([path], release_exists=True)
        path.write_text(
            json.dumps({"version": 1, "stages": {}, "current_signals": {}}), encoding="utf-8"
        )
        validate_restored_state([path], release_exists=True)


if __name__ == "__main__":
    unittest.main()
