from __future__ import annotations

import importlib.util
import json
import unittest
from unittest.mock import patch
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "check_required_checks.py"
SPEC = importlib.util.spec_from_file_location("check_required_checks", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check_required_checks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_required_checks)


class RequiredChecksTests(unittest.TestCase):
    @staticmethod
    def ruleset(*, conditions=None, rules=None, enforcement="active"):
        value = {
            "name": "Protect main",
            "enforcement": enforcement,
            "rules": rules if rules is not None else RequiredChecksTests.protective_rules(),
        }
        if conditions is not None:
            value["conditions"] = conditions
        return value

    @staticmethod
    def protective_rules(review_count=1):
        return [
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [
                        {"context": name} for name in sorted(check_required_checks.REQUIRED_CHECKS)
                    ]
                },
            },
            {
                "type": "pull_request",
                "parameters": {"required_approving_review_count": review_count},
            },
        ]

    def test_accepts_active_rules_with_canonical_and_compatibility_contexts(self) -> None:
        self.assertEqual(
            check_required_checks.validate_rulesets([self.ruleset()], "owner/repo"), []
        )

    def test_ignores_other_branch_and_tag_scopes(self) -> None:
        rules = self.protective_rules()
        unrelated = [
            self.ruleset(
                conditions={"ref_name": {"include": ["refs/heads/release/*"], "exclude": []}},
                rules=rules,
            ),
            self.ruleset(
                conditions={"ref_name": {"include": ["refs/tags/*"], "exclude": []}}, rules=rules
            ),
        ]
        errors = check_required_checks.validate_rulesets(unrelated, "owner/repo")
        self.assertIn("missing required status checks", errors[0])
        self.assertIn("no active ruleset requires pull requests", errors)

    def test_applies_inclusions_exclusions_and_repository_scope(self) -> None:
        rules = self.protective_rules()
        matching = self.ruleset(
            conditions={
                "ref_name": {"include": ["refs/heads/*"], "exclude": ["refs/heads/release/*"]},
                "repository_name": {"include": ["owner/*"], "exclude": ["owner/private"]},
            },
            rules=rules,
        )
        self.assertEqual(check_required_checks.validate_rulesets([matching], "owner/repo"), [])
        self.assertTrue(check_required_checks.validate_rulesets([matching], "other/repo"))

    def test_zero_approval_pull_request_rule_enforces_pr_based_merges(self) -> None:
        errors = check_required_checks.validate_rulesets(
            [self.ruleset(rules=self.protective_rules(0))]
        )
        self.assertEqual(errors, [])

    def test_positive_approval_pull_request_rule_is_accepted(self) -> None:
        errors = check_required_checks.validate_rulesets(
            [self.ruleset(rules=self.protective_rules(2))]
        )
        self.assertEqual(errors, [])

    def test_missing_pull_request_rule_fails_pr_enforcement(self) -> None:
        errors = check_required_checks.validate_rulesets(
            [self.ruleset(rules=self.protective_rules()[:1])]
        )
        self.assertIn("no active ruleset requires pull requests", errors)

    def test_inherited_unconditional_and_current_scoped_rulesets_combine(self) -> None:
        inherited = self.ruleset(rules=self.protective_rules())
        current = self.ruleset(
            conditions={"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
            rules=[{"type": "pull_request", "parameters": {"required_approving_review_count": 2}}],
        )
        self.assertEqual(
            check_required_checks.validate_rulesets([inherited, current], "owner/repo"), []
        )

    def test_unknown_scope_fails_closed(self) -> None:
        ruleset = self.ruleset(conditions={"ref_name": {"include": "*", "exclude": []}})
        errors = check_required_checks.validate_rulesets([ruleset], "owner/repo")
        self.assertTrue(any("unable to determine ruleset scope" in error for error in errors))

    @patch.object(check_required_checks.subprocess, "run")
    def test_fetch_rulesets_requests_and_combines_all_pages(self, run):
        run.side_effect = [
            type("Result", (), {"stdout": json.dumps([[{"id": 1}], [{"id": 2}]])})(),
            type("Result", (), {"stdout": '{"id": 1}'})(),
            type("Result", (), {"stdout": '{"id": 2}'})(),
        ]
        self.assertEqual(check_required_checks.fetch_rulesets("owner/repo"), [{"id": 1}, {"id": 2}])
        self.assertIn("--paginate", run.call_args_list[0].args[0])
        self.assertIn("--slurp", run.call_args_list[0].args[0])

    def test_reports_missing_checks_and_pull_request_rule(self) -> None:
        errors = check_required_checks.validate_rulesets(
            [{"name": "inactive", "enforcement": "disabled", "rules": []}]
        )
        self.assertEqual(len(errors), 2)
        self.assertIn("Unit tests + quality", errors[0])
        self.assertIn("no active ruleset requires pull requests", errors)


if __name__ == "__main__":
    unittest.main()
