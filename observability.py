#!/usr/bin/env python3
"""Structured runtime observability for production forecast workflows.

The module intentionally uses only the Python standard library so GitHub Actions
can initialize/finalize observability before project dependencies are installed.
It writes a compact machine-readable snapshot plus append-only JSONL events.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence


REPORT_PATH = Path("forecast_observability.json")
EVENT_LOG_PATH = Path("forecast_observability.jsonl")
TERMINAL_STATUSES = {"success", "failed", "skipped"}
DEFAULT_COUNTERS = {
    "skips": 0,
    "failures": 0,
    "fallbacks": 0,
    "data_quality_events": 0,
    "successful_posts": 0,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stable_run_id() -> str:
    """Return a run identifier shared by all processes in one Actions run."""
    explicit = os.getenv("BTC_RUN_ID")
    if explicit:
        return explicit
    github_run = os.getenv("GITHUB_RUN_ID")
    if github_run:
        attempt = os.getenv("GITHUB_RUN_ATTEMPT", "1")
        return f"github-{github_run}-{attempt}"
    return f"local-{os.getpid()}-{int(time.time())}"


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class PipelineObserver:
    """Persist structured stage timings, counters and correlated JSONL events."""

    def __init__(
        self,
        *,
        run_type: str = "production_forecast",
        report_path: Path = REPORT_PATH,
        event_log_path: Path = EVENT_LOG_PATH,
        run_id: str | None = None,
    ) -> None:
        self.report_path = report_path
        self.event_log_path = event_log_path
        resolved_run_id = run_id or stable_run_id()
        self.data: dict[str, Any]
        existing = self._load_existing()
        if existing and existing.get("run_id") == resolved_run_id:
            self.data = existing
            counters = self.data.setdefault("counters", {})
            for name, default in DEFAULT_COUNTERS.items():
                counters.setdefault(name, default)
            self.data.setdefault("stages", [])
            self.data.setdefault("metadata", {})
        else:
            now = utc_now()
            self.data: dict[str, Any] = {
                "schema_version": 1,
                "run_id": resolved_run_id,
                "run_type": run_type,
                "status": "running",
                "started_at": _iso(now),
                "finished_at": None,
                "duration_ms": None,
                "experiment_id": None,
                "counters": dict(DEFAULT_COUNTERS),
                "stages": [],
                "metadata": {},
            }
            self._persist()
            self.event("run_started", status="running", run_type=run_type)

    def _load_existing(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.report_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @property
    def run_id(self) -> str:
        return str(self.data["run_id"])

    def _persist(self) -> None:
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text(
            json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def event(self, event: str, *, status: str | None = None, **fields: Any) -> None:
        payload = {
            "timestamp": _iso(utc_now()),
            "run_id": self.run_id,
            "experiment_id": self.data.get("experiment_id"),
            "event": event,
            "status": status,
            **fields,
        }
        self.event_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.event_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
        print(json.dumps(payload, sort_keys=True))

    def set_experiment_id(self, experiment_id: str | None) -> None:
        if not experiment_id:
            return
        self.data["experiment_id"] = str(experiment_id)
        self._persist()
        self.event("experiment_linked", status="success", experiment_id=str(experiment_id))

    def metadata(self, **values: Any) -> None:
        self.data.setdefault("metadata", {}).update(values)
        self._persist()
        self.event("metadata_updated", status="success", **values)

    def increment(self, name: str, value: int = 1, **fields: Any) -> None:
        counters = self.data.setdefault("counters", {})
        counters[name] = int(counters.get(name, 0)) + int(value)
        self._persist()
        self.event(
            "counter", status="success", counter=name, delta=value, value=counters[name], **fields
        )

    def record_stage(
        self,
        name: str,
        *,
        status: str,
        duration_ms: float = 0.0,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        finished = finished_at or utc_now()
        started = started_at or finished
        entry = {
            "name": name,
            "status": status,
            "started_at": _iso(started),
            "finished_at": _iso(finished),
            "duration_ms": round(float(duration_ms), 3),
            **fields,
        }
        self.data.setdefault("stages", []).append(entry)
        self._persist()
        self.event(
            "stage_finished", status=status, stage=name, duration_ms=entry["duration_ms"], **fields
        )
        return entry

    @contextmanager
    def stage(self, name: str, **fields: Any) -> Iterator[None]:
        started_at = utc_now()
        started = time.perf_counter()
        self.event("stage_started", status="running", stage=name, **fields)
        try:
            yield
        except BaseException as exc:
            duration = (time.perf_counter() - started) * 1000.0
            self.data["counters"]["failures"] = int(self.data["counters"].get("failures", 0)) + 1
            self.record_stage(
                name,
                status="failed",
                duration_ms=duration,
                started_at=started_at,
                error_type=type(exc).__name__,
                error=str(exc),
                **fields,
            )
            raise
        else:
            duration = (time.perf_counter() - started) * 1000.0
            self.record_stage(
                name,
                status="success",
                duration_ms=duration,
                started_at=started_at,
                **fields,
            )

    def skip(self, reason: str) -> None:
        self.increment("skips", reason=reason)
        self.metadata(skip_reason=reason)
        self.finalize("skipped")

    def finalize(self, status: str, *, preserve_terminal: bool = False) -> None:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"unsupported final status: {status}")
        if preserve_terminal and self.data.get("status") in TERMINAL_STATUSES:
            return
        finished = utc_now()
        started = _parse_time(self.data.get("started_at"))
        self.data["status"] = status
        self.data["finished_at"] = _iso(finished)
        self.data["duration_ms"] = (
            round((finished - started).total_seconds() * 1000.0, 3) if started is not None else None
        )
        self._persist()
        self.event("run_finished", status=status, duration_ms=self.data.get("duration_ms"))

    def summary_markdown(self) -> str:
        counters = self.data.get("counters", {})
        lines = [
            "## Forecast observability",
            "",
            f"- Run: `{self.run_id}`",
            f"- Experiment: `{self.data.get('experiment_id') or 'not-yet-linked'}`",
            f"- Status: **{self.data.get('status', 'unknown')}**",
            f"- Total duration: **{self.data.get('duration_ms') if self.data.get('duration_ms') is not None else '--'} ms**",
            "",
            "| Stage | Status | Duration (ms) |",
            "| --- | --- | ---: |",
        ]
        for stage in self.data.get("stages", []):
            lines.append(
                f"| `{stage.get('name')}` | {stage.get('status')} | {stage.get('duration_ms', 0):.3f} |"
            )
        if not self.data.get("stages"):
            lines.append("| _none_ | -- | 0.000 |")
        lines.extend(
            [
                "",
                "Counters: "
                + ", ".join(f"`{name}={int(counters.get(name, 0))}`" for name in DEFAULT_COUNTERS),
            ]
        )
        if self.data.get("metadata", {}).get("skip_reason"):
            lines.append(f"Skip reason: {self.data['metadata']['skip_reason']}")
        return "\n".join(lines) + "\n"


def run_stage(
    observer: PipelineObserver, stage_name: str, command: Sequence[str], success_counter: str | None
) -> int:
    if not command:
        raise ValueError("run-stage requires a command after --")
    try:
        with observer.stage(stage_name, command=list(command)):
            completed = subprocess.run(list(command), check=False)
            if completed.returncode != 0:
                raise subprocess.CalledProcessError(completed.returncode, list(command))
    except subprocess.CalledProcessError as exc:
        return int(exc.returncode)
    if success_counter:
        observer.increment(success_counter, stage=stage_name)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Structured forecast pipeline observability")
    parser.add_argument("--report", type=Path, default=REPORT_PATH)
    parser.add_argument("--events", type=Path, default=EVENT_LOG_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--run-type", default="production_forecast")

    skip = sub.add_parser("skip")
    skip.add_argument("--reason", required=True)

    finalize = sub.add_parser("finalize")
    finalize.add_argument("--status", choices=sorted(TERMINAL_STATUSES), required=True)
    finalize.add_argument("--preserve-terminal", action="store_true")

    summary = sub.add_parser("summary")
    summary.add_argument("--append", type=Path)

    stage = sub.add_parser("run-stage")
    stage.add_argument("--stage", required=True)
    stage.add_argument("--success-counter")
    stage.add_argument("command_args", nargs=argparse.REMAINDER)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    observer = PipelineObserver(
        run_type=getattr(args, "run_type", "production_forecast"),
        report_path=args.report,
        event_log_path=args.events,
    )
    if args.command == "init":
        return
    if args.command == "skip":
        observer.skip(args.reason)
        return
    if args.command == "finalize":
        observer.finalize(args.status, preserve_terminal=args.preserve_terminal)
        return
    if args.command == "summary":
        rendered = observer.summary_markdown()
        if args.append:
            with args.append.open("a", encoding="utf-8") as handle:
                handle.write(rendered)
        else:
            print(rendered, end="")
        return
    if args.command == "run-stage":
        command = list(args.command_args)
        if command and command[0] == "--":
            command = command[1:]
        raise SystemExit(run_stage(observer, args.stage, command, args.success_counter))
    raise AssertionError(f"unhandled command {args.command}")


if __name__ == "__main__":
    main()
