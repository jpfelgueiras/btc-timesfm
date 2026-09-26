"""Workflow-level regression checks for production state restoration."""

from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW = Path(__file__).parents[2] / ".github/workflows/forecast.yml"


class ForecastWorkflowTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
