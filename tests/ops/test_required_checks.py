from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "check_required_checks.py"
SPEC = importlib.util.spec_from_file_location("check_required_checks", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check_required_checks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_required_checks)


class RequiredChecksTests(unittest.TestCase):
    def test_accepts_active_rules_with_canonical_and_compatibility_contexts(self) -> None:
        checks = sorted(check_required_checks.REQUIRED_CHECKS)
        rulesets = [
            {
                "name": "Protect main",
                "enforcement": "active",
                "rules": [
                    {
                        "type": "required_status_checks",
                        "parameters": {
                            "required_status_checks": [{"context": name} for name in checks]
                        },
                    },
                    {"type": "pull_request"},
                ],
            }
        ]
        self.assertEqual(check_required_checks.validate_rulesets(rulesets), [])

    def test_reports_missing_checks_and_review_rule(self) -> None:
        errors = check_required_checks.validate_rulesets(
            [{"name": "inactive", "enforcement": "disabled", "rules": []}]
        )
        self.assertEqual(len(errors), 2)
        self.assertIn("Unit tests + quality", errors[0])


if __name__ == "__main__":
    unittest.main()
