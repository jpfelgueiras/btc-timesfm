#!/usr/bin/env python3
"""Lightweight repository checks for accidental credential/log leakage."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


TEXT_SUFFIXES = {".py", ".yml", ".yaml", ".md", ".txt", ".toml", ".json"}
SKIP_PREFIXES = (".git/", ".venv/", "venv/")

SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "literal X auth_token": re.compile(
        r"""["']auth_token["']\s*:\s*["'](?!YOUR_|<|\$\{|\{\{)[A-Za-z0-9%._-]{20,}["']"""
    ),
    "literal X ct0": re.compile(
        r"""["']ct0["']\s*:\s*["'](?!YOUR_|<|\$\{|\{\{)[A-Za-z0-9%._-]{20,}["']"""
    ),
}

UNSAFE_WORKFLOW_PATTERNS = {
    "shell tracing": re.compile(r"(^|\s)set\s+-x(\s|$)"),
    "print all environment variables": re.compile(r"(^|\s)(?:printenv|env)(?:\s|$)"),
    "echo X cookie secret": re.compile(r"echo[^\n]*(?:X_COOKIES_JSON|auth_token|ct0)", re.I),
}


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return [Path(item.decode()) for item in result.stdout.split(b"\0") if item]


def audit_repository() -> list[str]:
    findings: list[str] = []
    for path in tracked_files():
        normalized = path.as_posix()
        if normalized.startswith(SKIP_PREFIXES) or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{normalized}: possible {label}")

        if normalized.startswith(".github/workflows/"):
            for label, pattern in UNSAFE_WORKFLOW_PATTERNS.items():
                if pattern.search(text):
                    findings.append(f"{normalized}: unsafe diagnostic pattern ({label})")
    return findings


def main() -> None:
    findings = audit_repository()
    if findings:
        print("Security repository audit failed:")
        for finding in findings:
            print(f"- {finding}")
        raise SystemExit(1)
    print("Security repository audit passed: no credential literals or unsafe log patterns found.")


if __name__ == "__main__":
    main()
