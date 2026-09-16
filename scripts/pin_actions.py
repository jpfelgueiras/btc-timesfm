from __future__ import annotations

import argparse
import subprocess
from collections.abc import Sequence


def resolve(action: str, revision: str) -> str:
    result = subprocess.run(
        ["git", "ls-remote", f"https://github.com/{action}.git", f"refs/tags/{revision}"],
        check=True,
        capture_output=True,
        text=True,
    )
    sha, _, _ = result.stdout.partition("\t")
    if len(sha) != 40:
        raise ValueError(f"No immutable tag found for {action}@{revision}")
    return sha


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve a GitHub Action tag to its commit SHA.")
    parser.add_argument("action")
    parser.add_argument("revision")
    arguments = parser.parse_args(argv)
    print(resolve(arguments.action, arguments.revision))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
