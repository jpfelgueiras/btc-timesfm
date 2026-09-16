"""Retry, record and quarantine policy for flaky unit tests."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

FAILURE_PATTERN = re.compile(r"^(?:FAIL|ERROR): [^ ]+ \((?P<qualified>[^)]+)\)$", re.MULTILINE)


@dataclass(frozen=True)
class Quarantine:
    test_id: str
    issue: int
    issue_open: bool


def failed_test_ids(output: str) -> list[str]:
    """Extract unique unittest identifiers from standard unittest output."""
    return list(
        dict.fromkeys(match.group("qualified") for match in FAILURE_PATTERN.finditer(output))
    )


def load_quarantines(path: Path, open_issues: set[int] | None = None) -> dict[str, Quarantine]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(payload, dict) or not isinstance(payload.get("tests", {}), dict):
        raise ValueError("quarantine file must contain a tests object")
    quarantines: dict[str, Quarantine] = {}
    for test_id, value in payload["tests"].items():
        if not isinstance(test_id, str) or not isinstance(value, dict):
            raise ValueError("each quarantine must map a test id to an object")
        issue = value.get("issue")
        if not isinstance(issue, int) or issue <= 0:
            raise ValueError(f"quarantine for {test_id} requires a positive issue number")
        quarantines[test_id] = Quarantine(
            test_id, issue, open_issues is None or issue in open_issues
        )
    return quarantines


def load_log(path: Path) -> dict[str, dict[str, int]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    tests = payload.get("tests", {}) if isinstance(payload, dict) else {}
    if not isinstance(tests, dict):
        return {}
    return {
        test_id: {key: int(value) for key, value in data.items() if isinstance(value, int)}
        for test_id, data in tests.items()
        if isinstance(test_id, str) and isinstance(data, dict)
    }


def record_outcomes(
    log: dict[str, dict[str, int]], failed: Sequence[str], rerun_failed: Sequence[str]
) -> dict[str, dict[str, int]]:
    rerun_failed_set = set(rerun_failed)
    for test_id in failed:
        entry = log.setdefault(test_id, {"failures": 0, "rerun_passes": 0, "rerun_failures": 0})
        entry["failures"] = entry.get("failures", 0) + 1
        outcome = "rerun_failures" if test_id in rerun_failed_set else "rerun_passes"
        entry[outcome] = entry.get(outcome, 0) + 1
    return log


def save_log(path: Path, log: dict[str, dict[str, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "tests": dict(sorted(log.items())),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def verdict(
    failed: Sequence[str], rerun_failed: Sequence[str], quarantines: dict[str, Quarantine]
) -> tuple[list[str], list[str]]:
    blocked: list[str] = []
    quarantined: list[str] = []
    for test_id in rerun_failed:
        quarantine = quarantines.get(test_id)
        if quarantine is not None and quarantine.issue_open:
            quarantined.append(test_id)
        else:
            blocked.append(test_id)
    return blocked, quarantined


def run_command(command: Sequence[str]) -> tuple[int, str]:
    completed = subprocess.run(command, check=False, text=True, capture_output=True)
    output = completed.stdout + completed.stderr
    print(output, end="")
    return completed.returncode, output


def retry_command(command: Sequence[str], failed: Sequence[str]) -> list[str]:
    if not command or Path(command[0]).name != "unittest-parallel":
        raise ValueError("retry requires unittest-parallel as the test command")
    return [command[0], "-t", ".", *failed]


def write_summary(
    path: Path | None,
    failed: Sequence[str],
    rerun_failed: Sequence[str],
    blocked: Sequence[str],
    quarantined: Sequence[str],
    log: dict[str, dict[str, int]],
) -> None:
    lines = ["## Flaky-test report", "", f"- Initial failures: **{len(failed)}**"]
    lines.append(f"- Rerun failures: **{len(rerun_failed)}**")
    lines.append(f"- Blocking failures: **{len(blocked)}**")
    lines.append(f"- Quarantined failures: **{len(quarantined)}**")
    for test_id in failed:
        counts = log[test_id]
        result = "failed" if test_id in rerun_failed else "passed"
        lines.append(
            f"- `{test_id}` rerun **{result}**; failures={counts['failures']}, "
            f"rerun_passes={counts['rerun_passes']}, rerun_failures={counts['rerun_failures']}"
        )
    rendered = "\n".join(lines) + "\n"
    if path is None:
        print(rendered, end="")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(rendered)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run unit tests once, retry failures once, and log outcomes"
    )
    parser.add_argument("--log", type=Path, default=Path(".state/flaky-tests.json"))
    parser.add_argument(
        "--quarantines", type=Path, default=Path(".github/flaky-test-quarantine.json")
    )
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--open-issue", type=int, action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("a test command is required after --")
    initial_returncode, output = run_command(command)
    failed = failed_test_ids(output)
    rerun_failed: list[str] = []
    unknown_failure = initial_returncode != 0 and not failed
    if failed:
        rerun_returncode, rerun_output = run_command(retry_command(command, failed))
        rerun_failed = failed_test_ids(rerun_output)
        unknown_failure = rerun_returncode != 0 and not rerun_failed
    log = record_outcomes(load_log(args.log), failed, rerun_failed)
    save_log(args.log, log)
    quarantines = load_quarantines(args.quarantines, set(args.open_issue))
    blocked, quarantined = verdict(failed, rerun_failed, quarantines)
    write_summary(args.summary, failed, rerun_failed, blocked, quarantined, log)
    raise SystemExit(bool(blocked or unknown_failure))


if __name__ == "__main__":
    main()
