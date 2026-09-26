from __future__ import annotations

import argparse
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


def validate_rulesets(rulesets: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    found_checks: set[str] = set()
    for ruleset in rulesets:
        if ruleset.get("enforcement") != "active":
            continue
        for rule in ruleset.get("rules", []):
            if rule.get("type") != "required_status_checks":
                continue
            for check in rule.get("parameters", {}).get("required_status_checks", []):
                context = check.get("context")
                if context:
                    found_checks.add(context)
    missing = sorted(REQUIRED_CHECKS - found_checks)
    if missing:
        errors.append("missing required status checks: " + ", ".join(missing))
    if not any(
        ruleset.get("enforcement") == "active"
        and any(rule.get("type") == "pull_request" for rule in ruleset.get("rules", []))
        for ruleset in rulesets
    ):
        errors.append("no active ruleset enforces pull-request review")
    return errors


def fetch_rulesets(repository: str) -> list[dict[str, Any]]:
    listing = subprocess.run(
        ["gh", "api", f"repos/{repository}/rulesets?includes_parents=true"],
        check=True,
        capture_output=True,
        text=True,
    )
    entries = json.loads(listing.stdout)
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
        errors = validate_rulesets(fetch_rulesets(repository))
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError, KeyError) as error:
        print(f"unable to verify GitHub rulesets: {error}", file=sys.stderr)
        return 2
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"Required merge checks are configured for {repository}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
