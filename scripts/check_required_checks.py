from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from typing import Any

REQUIRED_CHECKS = {
    "Unit tests + quality",
    "Unit tests",
    "unit-tests",
    "Dependency audit",
}


def _matches_scope(ruleset: dict[str, Any], repository: str) -> bool:
    conditions = ruleset.get("conditions", {})
    if not isinstance(conditions, dict):
        raise ValueError("ruleset conditions are not an object")

    def included(name: str, value: str, target: str) -> bool:
        if name not in conditions:
            return True
        scope = conditions[name]
        if not isinstance(scope, dict):
            raise ValueError(f"ruleset {name} condition is not an object")
        include = scope.get("include")
        exclude = scope.get("exclude")
        if not isinstance(include, list) or not isinstance(exclude, list):
            raise ValueError(f"ruleset {name} condition lacks include/exclude lists")

        def matches(pattern: Any) -> bool:
            if not isinstance(pattern, str):
                raise ValueError(f"ruleset {name} condition contains a non-string pattern")
            if pattern == "~ALL":
                return True
            if pattern == "~DEFAULT_BRANCH":
                return target == "refs/heads/main"
            if pattern.startswith("~"):
                raise ValueError(f"unsupported ruleset {name} pattern: {pattern}")
            return fnmatch.fnmatchcase(value, pattern)

        return any(matches(pattern) for pattern in include) and not any(
            matches(pattern) for pattern in exclude
        )

    return included("ref_name", "refs/heads/main", "refs/heads/main") and included(
        "repository_name", repository, "refs/heads/main"
    )


def validate_rulesets(rulesets: list[dict[str, Any]], repository: str = "") -> list[str]:
    errors: list[str] = []
    found_checks: set[str] = set()
    applies_review = False
    for ruleset in rulesets:
        if ruleset.get("enforcement") != "active":
            continue
        try:
            if not _matches_scope(ruleset, repository):
                continue
        except ValueError as error:
            errors.append(f"unable to determine ruleset scope: {error}")
            continue
        for rule in ruleset.get("rules", []):
            if rule.get("type") == "pull_request":
                count = rule.get("parameters", {}).get("required_approving_review_count", 0)
                if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                    applies_review = True
            if rule.get("type") != "required_status_checks":
                continue
            for check in rule.get("parameters", {}).get("required_status_checks", []):
                context = check.get("context")
                if context:
                    found_checks.add(context)
    missing = sorted(REQUIRED_CHECKS - found_checks)
    if missing:
        errors.append("missing required status checks: " + ", ".join(missing))
    if not applies_review:
        errors.append("no active ruleset enforces pull-request review")
    return errors


def fetch_rulesets(repository: str) -> list[dict[str, Any]]:
    listing = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", f"repos/{repository}/rulesets?includes_parents=true"],
        check=True,
        capture_output=True,
        text=True,
    )
    pages = json.loads(listing.stdout)
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ValueError("paginated ruleset listing is not a list of pages")
    entries = [entry for page in pages for entry in page]
    rulesets = []
    for entry in entries:
        detail = subprocess.run(
            ["gh", "api", f"repos/{repository}/rulesets/{entry['id']}"],
            check=True,
            capture_output=True,
            text=True,
        )
        rulesets.append(json.loads(detail.stdout))
    return rulesets


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify required GitHub ruleset checks.")
    parser.add_argument("--repo", help="GitHub OWNER/REPOSITORY (defaults to gh repository view)")
    args = parser.parse_args()
    try:
        repository = args.repo or (
            subprocess.run(
                ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        errors = validate_rulesets(fetch_rulesets(repository), repository)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError, KeyError, ValueError) as error:
        print(f"unable to verify GitHub rulesets: {error}", file=sys.stderr)
        return 2
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"Required merge checks are configured for {repository}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
