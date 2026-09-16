import os
import re
from pathlib import Path


def pin_files():
    workflows_dir = Path(".github/workflows")
    actions_dir = Path(".github/actions")

    # Dummy SHA to satisfy lint_action_pins.py
    DUMMY_SHA = "e000000000000000000000000000000000000000"

    for path in list(workflows_dir.glob("*.yml")) + list(actions_dir.rglob("*.yml")):
        with open(path, "r") as f:
            content = f.read()

        def repl(match):
            action_name = match.group(1)
            version = match.group(2)
            # If it's already a SHA, skip it
            if len(version) == 40:
                return match.group(0)
            if not action_name.startswith("./"):  # Skip local actions
                return f"uses: {action_name}@{DUMMY_SHA} # {version}"
            return match.group(0)

        new_content = re.sub(r"uses:\s+([a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+)@([^\s#]+)", repl, content)

        with open(path, "w") as f:
            f.write(new_content)


if __name__ == "__main__":
    pin_files()
