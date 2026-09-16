import os
import re
import sys
from pathlib import Path


def is_pinned(action_str):
    # Check if it has a SHA (usually 40 hex chars)
    # This is a simple heuristic.
    return re.search(r"@[a-f0-9]{40}", action_str) is not None


def scan_files():
    workflows_dir = Path(".github/workflows")
    actions_dir = Path(".github/actions")

    unpinned_found = False

    for path in list(workflows_dir.glob("*.yml")) + list(actions_dir.rglob("*.yml")):
        with open(path, "r") as f:
            content = f.read()
            # Match actions usage: uses: actions/checkout@vX or uses: ./path/to/action
            # We care about third-party ones, so maybe exclude ./local-actions
            matches = re.finditer(r"uses:\s+([a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+)@([^\s]+)", content)

            for match in matches:
                action = match.group(0)
                if not is_pinned(action):
                    print(f"Unpinned action found in {path}: {action}")
                    unpinned_found = True

    return unpinned_found


if __name__ == "__main__":
    if scan_files():
        sys.exit(1)
    sys.exit(0)
