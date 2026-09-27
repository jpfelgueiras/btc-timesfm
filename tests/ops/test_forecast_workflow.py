"""Workflow-level regression checks for production state restoration."""

from __future__ import annotations

import gzip
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


WORKFLOW = Path(__file__).parents[2] / ".github/workflows/forecast.yml"


class ForecastWorkflowTests(unittest.TestCase):
    def _shadow_cli(self, db: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        repository = WORKFLOW.parents[2]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(repository / "src")
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "btc_timesfm.research.shadow_deployment",
                "--db",
                str(db),
                *arguments,
            ],
            cwd=repository,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_independent_backup_endpoint_is_configured_across_workflows(self) -> None:
        repository = WORKFLOW.parents[1].parent
        for name in (
            "forecast.yml",
            "independent-history-backup-monitor.yml",
            "disaster-recovery-drill.yml",
        ):
            with self.subTest(workflow=name):
                workflow = (repository / ".github/workflows" / name).read_text(encoding="utf-8")
                self.assertIn(
                    "HISTORY_BACKUP_AWS_ENDPOINT_URL: ${{ vars.HISTORY_BACKUP_AWS_ENDPOINT_URL }}",
                    workflow,
                )

    def test_durable_restore_reuses_schedule_gate_registry_and_validates_it(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        production_restore = workflow.split("      - name: Restore durable production state", 1)[1]
        production_restore = production_restore.split("      - name:", 1)[0]

        self.assertIn("if [ ! -f .state/x_post_registry.json ]; then", production_restore)
        self.assertIn("--pattern 'x_post_registry.json'", production_restore)
        self.assertIn("validate_restored_state(", production_restore)
        self.assertIn("Path('.state/x_post_registry.json')", production_restore)
        self.assertIn("release_exists=True", production_restore)

    def test_established_release_missing_shadow_asset_fails_without_initializing(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        established = workflow.split("- name: Restore durable production state", 1)[1]
        established = established.split("      - name:", 1)[0]
        self.assertIn("--pattern 'shadow_deployment.sqlite.gz'", established)
        self.assertIn("--db .state/shadow_deployment.sqlite restore", established)
        self.assertIn("--archive .state/shadow_deployment.sqlite.gz", established)
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "shadow.sqlite"
            missing_archive = Path(directory) / "missing.sqlite.gz"
            result = self._shadow_cli(db, "restore", "--archive", str(missing_archive))
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(db.exists())

    def test_established_release_corrupt_shadow_archive_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "shadow.sqlite.gz"
            with gzip.open(archive, "wb") as compressed:
                compressed.write(b"not a SQLite database")
            db = root / "shadow.sqlite"
            result = self._shadow_cli(db, "restore", "--archive", str(archive))
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(db.exists())

    def test_first_ever_release_absence_initializes_schema_v6(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        first_run = workflow.split('echo "No durable history release exists yet', 1)[1]
        self.assertIn("init-first-run", first_run)
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "shadow.sqlite"
            result = self._shadow_cli(db, "init-first-run")
            self.assertEqual(result.returncode, 0, result.stderr)
            verification = self._shadow_cli(db, "verify")
            self.assertEqual(verification.returncode, 0, verification.stderr)

    def test_shadow_evidence_is_uploaded_monitored_and_restored_independently(self) -> None:
        repository = WORKFLOW.parents[1].parent
        forecast = WORKFLOW.read_text(encoding="utf-8")
        monitor = (
            repository / ".github/workflows/independent-history-backup-monitor.yml"
        ).read_text(encoding="utf-8")
        drill = (repository / ".github/workflows/disaster-recovery-drill.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("--database-type shadow_deployment", forecast)
        self.assertIn("${HISTORY_INDEPENDENT_BACKUP_URI}.shadow_deployment", forecast)
        self.assertIn('"${HISTORY_INDEPENDENT_BACKUP_URI}.shadow_deployment"', monitor)
        self.assertIn("shadow-restored.sqlite restore", drill)
        self.assertIn("shadow-restored.sqlite verify", drill)


if __name__ == "__main__":
    unittest.main()
