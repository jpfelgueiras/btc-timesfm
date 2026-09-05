#!/usr/bin/env python3
"""One-time integration patch for issue #19."""

from __future__ import annotations

import re
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Expected exactly one {label} anchor, found {count}")
    return text.replace(old, new, 1)


history_path = Path("history_store.py")
history = history_path.read_text(encoding="utf-8")
history = replace_once(
    history,
    "from typing import Any, Iterable, TextIO\n\n\nSCHEMA_VERSION = 1\n",
    "from typing import Any, Iterable, TextIO\n\nfrom history_migrations import (\n    CURRENT_SCHEMA_VERSION,\n    migrate_database,\n    schema_diagnostics,\n    validate_database,\n)\n\n\nSCHEMA_VERSION = CURRENT_SCHEMA_VERSION\n",
    "history migration import",
)

history, init_count = re.subn(
    r"    def _initialize\(self\) -> None:\n.*?(?=    def ingest_snapshot\()",
    "    def _initialize(self) -> None:\n        migrate_database(self.path)\n\n",
    history,
    count=1,
    flags=re.S,
)
if init_count != 1:
    raise RuntimeError(f"Expected one _initialize method, replaced {init_count}")

history = replace_once(
    history,
    '        return {\n            "schema_version": SCHEMA_VERSION,\n            "origins": origins,',
    '        diagnostics = schema_diagnostics(self.path)\n        return {\n            "schema_version": diagnostics["schema_version"],\n            "supported_schema_version": diagnostics["supported_schema_version"],\n            "applied_migrations": diagnostics["applied_migrations"],\n            "origins": origins,',
    "stats diagnostics",
)

history, verify_count = re.subn(
    r"    def verify\(self\) -> dict\[str, Any\]:\n.*?(?=    def export_rows\()",
    "    def verify(self) -> dict[str, Any]:\n        return validate_database(self.path)\n\n",
    history,
    count=1,
    flags=re.S,
)
if verify_count != 1:
    raise RuntimeError(f"Expected one verify method, replaced {verify_count}")

history_path.write_text(history, encoding="utf-8")
