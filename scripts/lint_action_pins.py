from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

SHA = re.compile(r"^[0-9a-fA-F]{40}$")
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*(?P<reference>\S+)")
DEFAULT_PATHS = (Path(".github/workflows"), Path(".github/actions"))


def workflow_files(paths: Iterable[Path]) -> Iterable[Path]:
    for path in paths:
        if path.is_file() and path.suffix in {".yml", ".yaml"}:
            yield path
        elif path.is_dir():
            yield from sorted(
                candidate for candidate in path.rglob("*") if candidate.suffix in {".yml", ".yaml"}
            )


def validate(path: Path) -> list[str]:
    errors: list[str] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        match = USES.match(line)
        if match is None:
            continue
        reference = match.group("reference").strip("'\"")
        if reference.startswith(("./", "../")):
            continue
        action, separator, revision = reference.rpartition("@")
        if not separator or not action or not SHA.fullmatch(revision):
            errors.append(
                f"{path}:{line_number}: action must be pinned to a full commit SHA: {reference}"
            )
    return errors


def lint(paths: Iterable[Path]) -> list[str]:
    return [error for path in workflow_files(paths) for error in validate(path)]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Require immutable SHA pins for GitHub Actions.")
    parser.add_argument("paths", nargs="*", type=Path, default=DEFAULT_PATHS)
    arguments = parser.parse_args(argv)
    errors = lint(arguments.paths)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
