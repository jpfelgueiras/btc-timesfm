"""Gap-threshold alerting built on the upstream data reconciliation report.

Monitors the machine-readable ``gap_reconciliation.json`` summary and fires
alerts when gap counts, silent-fill candidates, missing candles, or
withhold-required gaps cross configured thresholds. Alerts are emitted through
the structured observability module so they land in the JSONL event log, the
observability report counters, and any subscriber of ``forecast_observability``.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from btc_timesfm.ops.observability import PipelineObserver


GAP_ALERT_SCHEMA_VERSION = 1
DEFAULT_REPORT_PATH = Path("gap_reconciliation.json")
DEFAULT_ALERT_STATE_PATH = Path(".state/gap_alert_state.json")
DEFAULT_ALERT_LOG_PATH = Path("gap_alert_events.jsonl")
DEFAULT_ALERT_STATUS_PATH = Path("gap_alert_status.json")
GAP_RUNBOOK_URL = "https://github.com/jpfelgueiras/btc-timesfm/blob/main/docs/MARKET_DATA.md"

SEVERITY_OK = "ok"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"


@dataclass(frozen=True)
class GapAlertConfig:
    warning_gaps_detected: int = 3
    critical_gaps_detected: int = 6
    warning_silent_fills: int = 2
    critical_silent_fills: int = 5
    warning_missing_candles: int = 4
    critical_missing_candles: int = 12
    warning_withhold_gaps: int = 0
    critical_withhold_gaps: int = 1
    alert_dedup_minutes: int = 60

    def __post_init__(self) -> None:
        ranges = (
            ("warning_gaps_detected", "critical_gaps_detected"),
            ("warning_silent_fills", "critical_silent_fills"),
            ("warning_missing_candles", "critical_missing_candles"),
            ("warning_withhold_gaps", "critical_withhold_gaps"),
        )
        for warning_name, critical_name in ranges:
            warning_value = getattr(self, warning_name)
            critical_value = getattr(self, critical_name)
            if warning_value < 0 or critical_value < 0:
                raise ValueError(f"{warning_name}/{critical_name} must be >= 0")
            if critical_value < warning_value:
                raise ValueError(f"{critical_name} must be >= {warning_name}")
        if self.alert_dedup_minutes < 1:
            raise ValueError("alert_dedup_minutes must be >= 1")

    @classmethod
    def from_env(cls) -> "GapAlertConfig":
        return cls(
            warning_gaps_detected=_env_int("BTC_GAP_ALERT_WARN_GAPS", cls.warning_gaps_detected),
            critical_gaps_detected=_env_int(
                "BTC_GAP_ALERT_CRITICAL_GAPS", cls.critical_gaps_detected
            ),
            warning_silent_fills=_env_int(
                "BTC_GAP_ALERT_WARN_SILENT_FILLS", cls.warning_silent_fills
            ),
            critical_silent_fills=_env_int(
                "BTC_GAP_ALERT_CRITICAL_SILENT_FILLS", cls.critical_silent_fills
            ),
            warning_missing_candles=_env_int(
                "BTC_GAP_ALERT_WARN_MISSING_CANDLES", cls.warning_missing_candles
            ),
            critical_missing_candles=_env_int(
                "BTC_GAP_ALERT_CRITICAL_MISSING_CANDLES", cls.critical_missing_candles
            ),
            warning_withhold_gaps=_env_int(
                "BTC_GAP_ALERT_WARN_WITHHOLD_GAPS", cls.warning_withhold_gaps
            ),
            critical_withhold_gaps=_env_int(
                "BTC_GAP_ALERT_CRITICAL_WITHHOLD_GAPS", cls.critical_withhold_gaps
            ),
            alert_dedup_minutes=_env_int("BTC_GAP_ALERT_DEDUP_MINUTES", cls.alert_dedup_minutes),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GapAlert:
    level: str
    metric: str
    value: int
    threshold: int
    message: str
    runbook_url: str = GAP_RUNBOOK_URL

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return int(default)
    value = int(raw)
    if value < 0:
        raise ValueError(f"{name} must be >= 0")
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def evaluate_gap_alerts(
    report: dict[str, Any],
    *,
    config: GapAlertConfig | None = None,
) -> dict[str, Any]:
    """Evaluate a reconciliation report against gap thresholds.

    Returns a machine-readable evaluation with an overall ``level`` and every
    fired ``alerts`` entry. Thresholds are strict: a metric only alerts once its
    value reaches the configured count.
    """
    cfg = config or GapAlertConfig.from_env()
    summary = report.get("summary") if isinstance(report, dict) else None
    if not isinstance(summary, dict):
        summary = {}

    metrics = {
        "gaps_detected": _to_int(summary.get("gaps_detected")),
        "missing_candles_total": _to_int(summary.get("missing_candles_total")),
        "silent_fill_candidates": _to_int(summary.get("silent_fill_candidates")),
        "withhold_gaps": _to_int(summary.get("withhold_gaps")),
    }
    checks = (
        (
            "gaps_detected",
            cfg.warning_gaps_detected,
            cfg.critical_gaps_detected,
            "primary data gaps detected",
        ),
        (
            "missing_candles_total",
            cfg.warning_missing_candles,
            cfg.critical_missing_candles,
            "total missing candles across providers",
        ),
        (
            "silent_fill_candidates",
            cfg.warning_silent_fills,
            cfg.critical_silent_fills,
            "silent-fill candidates without provenance",
        ),
        (
            "withhold_gaps",
            cfg.warning_withhold_gaps,
            cfg.critical_withhold_gaps,
            "gaps that require withholding the forecast",
        ),
    )

    alerts: list[GapAlert] = []
    overall = SEVERITY_OK
    for metric, warning_threshold, critical_threshold, label in checks:
        value = metrics[metric]
        if value >= critical_threshold:
            level = SEVERITY_CRITICAL
            threshold = critical_threshold
        elif warning_threshold > 0 and value >= warning_threshold:
            level = SEVERITY_WARNING
            threshold = warning_threshold
        else:
            continue
        alerts.append(
            GapAlert(
                level=level,
                metric=metric,
                value=value,
                threshold=threshold,
                message=(
                    f"{label} = {value} "
                    f"(warning at {warning_threshold}, critical at {critical_threshold})"
                ),
            )
        )
        if level == SEVERITY_CRITICAL:
            overall = SEVERITY_CRITICAL
        elif overall != SEVERITY_CRITICAL:
            overall = SEVERITY_WARNING

    policy = summary.get("policy")
    withhold_requested = False
    if isinstance(policy, dict) and policy.get("withhold_forecast"):
        withhold_requested = True
    if metrics["withhold_gaps"] > 0:
        withhold_requested = True
    if withhold_requested and not any(alert.metric == "withhold_gaps" for alert in alerts):
        alerts.append(
            GapAlert(
                level=SEVERITY_CRITICAL,
                metric="forecast_withheld",
                value=max(metrics["withhold_gaps"], 1),
                threshold=cfg.critical_withhold_gaps,
                message=(
                    "gap policy requires withholding the forecast "
                    f"(critical at withhold_gaps >= {cfg.critical_withhold_gaps})"
                ),
            )
        )
        overall = SEVERITY_CRITICAL

    return {
        "schema_version": GAP_ALERT_SCHEMA_VERSION,
        "level": overall,
        "alerts": [alert.as_dict() for alert in alerts],
        "metrics": metrics,
        "policy_withhold_forecast": withhold_requested,
        "configuration": cfg.as_dict(),
    }


def _to_int(value: object) -> int:
    if value is None:
        return 0
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def publish_gap_alerts(
    observer: PipelineObserver,
    report: dict[str, Any],
    *,
    config: GapAlertConfig | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate the report and surface alerts through the observability module."""
    evaluation = evaluate_gap_alerts(report, config=config)
    checked = _iso(now or _utc_now())
    alerts = evaluation.get("alerts", [])
    summary = report.get("summary") if isinstance(report, dict) else {}
    gap_count = summary.get("gaps_detected") if isinstance(summary, dict) else None
    if not isinstance(alerts, list) or not alerts:
        observer.event(
            "gap_health",
            status=SEVERITY_OK,
            gap_count=_to_int(gap_count),
            evaluated_at=checked,
        )
        return evaluation

    observer.increment("data_quality_events", value=len(alerts), source="gap_reconciliation")
    observer.increment("gap_alerts", value=len(alerts))
    for alert in alerts:
        observer.event(
            "gap_alert",
            status=str(alert.get("level")),
            metric=str(alert.get("metric")),
            value=_to_int(alert.get("value")),
            threshold=_to_int(alert.get("threshold")),
            message=str(alert.get("message")),
            runbook_url=GAP_RUNBOOK_URL,
            evaluated_at=checked,
        )
    return evaluation


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "version": GAP_ALERT_SCHEMA_VERSION,
            "alerts": {},
            "last_run_at": None,
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("alerts", {})
    payload.setdefault("last_run_at", None)
    return payload


def _save_state(state: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _append_log(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _should_alert(
    key: str,
    now: datetime,
    dedup_minutes: int,
    state: dict[str, Any],
) -> bool:
    entry = state.get("alerts", {}).get(key)
    if not isinstance(entry, dict):
        return True
    last_at = _parse_time(entry.get("last_alerted_at"))
    if last_at is None:
        return True
    elapsed = (now - last_at).total_seconds() / 60.0
    return elapsed >= dedup_minutes


def _update_dedup(state: dict[str, Any], key: str, now: datetime) -> None:
    entry = state.get("alerts", {}).get(key)
    count = 0
    if isinstance(entry, dict):
        count = int(entry.get("alert_count", 0))
    state["alerts"][key] = {"last_alerted_at": _iso(now), "alert_count": count + 1}


class GapAlertMonitor:
    """Deduplicated gap-alert monitor with an append-only fired-event log."""

    def __init__(
        self,
        *,
        state_path: Path | str = DEFAULT_ALERT_STATE_PATH,
        log_path: Path | str = DEFAULT_ALERT_LOG_PATH,
        config: GapAlertConfig | None = None,
    ) -> None:
        self.state_path = Path(state_path)
        self.log_path = Path(log_path)
        self.config = config or GapAlertConfig.from_env()
        self.state = _load_state(self.state_path)

    def record(
        self,
        report: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Record a report, firing any newly-breachable alerts."""
        checked = now or _utc_now()
        evaluation = evaluate_gap_alerts(report, config=self.config)
        emitted: list[dict[str, Any]] = []
        for alert in evaluation.get("alerts", []):
            key = f"gap-alert:{alert['metric']}:{alert['level']}"
            if not _should_alert(key, checked, self.config.alert_dedup_minutes, self.state):
                continue
            _update_dedup(self.state, key, checked)
            payload = {
                "timestamp": _iso(checked),
                "runbook_url": GAP_RUNBOOK_URL,
                **alert,
            }
            _append_log(payload, self.log_path)
            print(json.dumps(payload, sort_keys=True))
            emitted.append(payload)
        self.state["last_run_at"] = _iso(checked)
        _save_state(self.state, self.state_path)
        evaluation["alerts_emitted"] = emitted
        return evaluation


def build_status(
    report: dict[str, Any],
    evaluation: dict[str, Any],
    *,
    status_path: Path | str = DEFAULT_ALERT_STATUS_PATH,
) -> dict[str, Any]:
    """Write and return the machine-readable gap-alert status snapshot."""
    output = {
        "schema_version": GAP_ALERT_SCHEMA_VERSION,
        "evaluated_at": _iso(_utc_now()),
        "level": evaluation.get("level"),
        "alerts": evaluation.get("alerts", []),
        "metrics": evaluation.get("metrics", {}),
        "policy_withhold_forecast": evaluation.get("policy_withhold_forecast"),
        "summary": report.get("summary", {}) if isinstance(report, dict) else {},
    }
    path = Path(status_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def main() -> None:
    """CLI entrypoint: evaluate the latest reconciliation report and print status."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Evaluate gap-alert thresholds from the reconciliation report"
    )
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--status", type=Path, default=DEFAULT_ALERT_STATUS_PATH)
    args = parser.parse_args()
    if not args.report.exists():
        print(f"gap reconciliation report not found: {args.report}", file=sys.stderr)
        raise SystemExit(1)
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"failed to read {args.report}: {exc}", file=sys.stderr)
        raise SystemExit(1)
    if not isinstance(report, dict):
        print(f"unexpected reconciliation report shape in {args.report}", file=sys.stderr)
        raise SystemExit(1)
    evaluation = evaluate_gap_alerts(report)
    status = build_status(report, evaluation, status_path=args.status)
    print(json.dumps(status, indent=2, sort_keys=True))
    if evaluation.get("level") == SEVERITY_CRITICAL:
        raise SystemExit(2)
    if evaluation.get("level") == SEVERITY_WARNING:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
