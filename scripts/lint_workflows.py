from __future__ import annotations

import sys
from pathlib import Path

WORKFLOWS = Path(".github/workflows")
REQUIRED_WORKFLOW_FIELDS = ("permissions:", "concurrency:")
REQUIRED_JOB_FIELDS = ("runs-on:", "timeout-minutes:")


def job_blocks(lines: list[str]) -> list[tuple[str, list[str]]]:
    blocks: list[tuple[str, list[str]]] = []
    current_name: str | None = None
    current_lines: list[str] = []
    in_jobs = False
    for line in lines:
        if line == "jobs:\n":
            in_jobs = True
            continue
        if in_jobs and line and not line.startswith(" "):
            break
        if (
            in_jobs
            and line.startswith("  ")
            and not line.startswith("    ")
            and line.rstrip().endswith(":")
        ):
            if current_name is not None:
                blocks.append((current_name, current_lines))
            current_name = line.strip()[:-1]
            current_lines = [line]
        elif current_name is not None:
            current_lines.append(line)
    if current_name is not None:
        blocks.append((current_name, current_lines))
    return blocks


def validate(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    text = "".join(lines)
    errors = [
        f"{path}: missing workflow {field}"
        for field in REQUIRED_WORKFLOW_FIELDS
        if field not in text
    ]
    for name, block in job_blocks(lines):
        body = "".join(block)
        if "uses: ./.github/actions/python-setup" in body:
            errors.extend(
                f"{path}: job {name} missing {field}"
                for field in REQUIRED_JOB_FIELDS
                if field not in body
            )
            checkout_index = body.find("uses: actions/checkout@")
            setup_index = body.find("uses: ./.github/actions/python-setup")
            if checkout_index == -1 or checkout_index > setup_index:
                errors.append(
                    f"{path}: job {name} must check out the repository before shared Python setup"
                )
        if "uses: actions/upload-artifact@" in body and "retention-days:" not in body:
            errors.append(f"{path}: job {name} artifact is missing retention-days")
        if "uses: actions/setup-python@" in body:
            errors.append(f"{path}: job {name} bypasses the shared Python setup composite")
    return errors


def main() -> int:
    errors = [error for path in sorted(WORKFLOWS.glob("*.yml")) for error in validate(path)]
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
