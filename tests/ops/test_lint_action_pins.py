from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[2] / "scripts" / "lint_action_pins.py"
SPEC = importlib.util.spec_from_file_location("lint_action_pins", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
lint_action_pins = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lint_action_pins)


class LintActionPinsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_workflow(self, content: str) -> Path:
        path = self.root / "workflow.yml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_accepts_full_sha_and_local_action(self) -> None:
        path = self.write_workflow(
            "steps:\n"
            "  - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7\n"
            "  - uses: ./.github/actions/python-setup\n"
        )

        self.assertEqual(lint_action_pins.validate(path), [])

    def test_rejects_tags_branches_and_missing_revisions(self) -> None:
        path = self.write_workflow(
            "steps:\n"
            "  - uses: actions/checkout@v7\n"
            "  - uses: owner/action@main\n"
            "  - uses: owner/action\n"
        )

        errors = lint_action_pins.validate(path)

        self.assertEqual(len(errors), 3)
        self.assertTrue(all("full commit SHA" in error for error in errors))

    def test_rejects_abbreviated_and_non_hex_shas(self) -> None:
        path = self.write_workflow(
            "steps:\n"
            "  - uses: owner/action@3d3c42e\n"
            "  - uses: owner/action@gggggggggggggggggggggggggggggggggggggggg\n"
        )

        self.assertEqual(len(lint_action_pins.validate(path)), 2)

    def test_lint_recurses_through_action_directories(self) -> None:
        action = self.root / "actions" / "nested" / "action.yaml"
        action.parent.mkdir(parents=True)
        action.write_text("runs:\n  steps:\n    - uses: owner/action@v1\n", encoding="utf-8")

        self.assertEqual(len(lint_action_pins.lint([self.root / "actions"])), 1)
