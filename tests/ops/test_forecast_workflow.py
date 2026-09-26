"""Workflow-level regression checks for production state restoration."""

from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW = Path(__file__).parents[2] / ".github/workflows/forecast.yml"


class ForecastWorkflowTests(unittest.TestCase):
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
