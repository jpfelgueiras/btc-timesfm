#!/usr/bin/env python3
"""Generate forecast-performance dashboard artifacts from durable history."""

from __future__ import annotations

import argparse
import html
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from history_store import DEFAULT_DB_PATH, ENSEMBLE_MODEL, ForecastHistoryStore

DEFAULT_HORIZONS = (2, 4, 8, 16)
DEFAULT_ROLLING_DAYS = (7, 30, 90)
DEFAULT_LOW_SAMPLE_THRESHOLD = 20
PERSISTENCE_MODEL = "persistence"


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp must be a non-empty string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    if not items:
        return None
    return sum(items) / len(items)


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def _metric_summary(rows: list[dict[str, Any]], low_sample_threshold: int) -> dict[str, Any]:
    samples = len(rows)
    interval_values = [
        float(value)
        for row in rows
        if (value := _safe_float(row.get("within_q10_q90"))) is not None
    ]
    warning: str | None = None
    if samples == 0:
        warning = "no_samples"
    elif samples < low_sample_threshold:
        warning = f"low_sample_count:{samples}<{low_sample_threshold}"

    return {
        "samples": samples,
        "mae_pct": _round(
            _mean(
                value
                for row in rows
                if (value := _safe_float(row.get("absolute_error_pct"))) is not None
            )
        ),
        "mean_signed_error_pct": _round(
            _mean(
                value
                for row in rows
                if (value := _safe_float(row.get("signed_error_pct"))) is not None
            )
        ),
        "direction_accuracy": _round(
            _mean(
                value
                for row in rows
                if (value := _safe_float(row.get("direction_correct"))) is not None
            )
        ),
        "q10_q90_coverage": _round(_mean(interval_values)),
        "interval_samples": len(interval_values),
        "confidence_warning": warning,
    }


def _sort_models(names: Iterable[str]) -> list[str]:
    priority = {ENSEMBLE_MODEL: 0, PERSISTENCE_MODEL: 1}
    return sorted(set(names), key=lambda name: (priority.get(name, 2), name))


def _window_rows(
    rows: list[dict[str, Any]],
    *,
    now: datetime,
    days: int | None,
) -> list[dict[str, Any]]:
    if days is None:
        return rows
    cutoff = now - timedelta(days=days)
    return [
        row for row in rows if row.get("origin_at") and _parse_timestamp(row["origin_at"]) >= cutoff
    ]


def _horizon_report(
    rows: list[dict[str, Any]],
    *,
    horizon: int,
    low_sample_threshold: int,
) -> dict[str, Any]:
    horizon_rows = [row for row in rows if int(row["horizon_hours"]) == horizon]
    model_names = _sort_models(
        [str(row["model_name"]) for row in horizon_rows] + [ENSEMBLE_MODEL, PERSISTENCE_MODEL]
    )
    models = {
        name: _metric_summary(
            [row for row in horizon_rows if str(row["model_name"]) == name],
            low_sample_threshold,
        )
        for name in model_names
    }

    regimes = sorted(
        {
            str(row.get("regime") or "unknown")
            for row in horizon_rows
            if row.get("regime") is not None
        }
        or {"unknown"}
    )
    by_regime: dict[str, Any] = {}
    for regime in regimes:
        regime_rows = [row for row in horizon_rows if str(row.get("regime") or "unknown") == regime]
        by_regime[regime] = {
            name: _metric_summary(
                [row for row in regime_rows if str(row["model_name"]) == name],
                low_sample_threshold,
            )
            for name in model_names
        }

    persistence_missing = models[PERSISTENCE_MODEL]["samples"] == 0
    return {
        "models": models,
        "by_regime": by_regime,
        "persistence_baseline_missing": persistence_missing,
    }


def build_report(
    rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    rolling_days: tuple[int, ...] = DEFAULT_ROLLING_DAYS,
    low_sample_threshold: int = DEFAULT_LOW_SAMPLE_THRESHOLD,
) -> dict[str, Any]:
    """Build a JSON-serializable dashboard report from exported history rows."""
    if low_sample_threshold < 1:
        raise ValueError("low_sample_threshold must be >= 1")
    if any(day < 1 for day in rolling_days):
        raise ValueError("rolling_days must contain positive integers")

    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    matured_rows = [row for row in rows if row.get("actual_target_price_usd") is not None]
    horizons = sorted(
        set(DEFAULT_HORIZONS)
        | {
            int(row["horizon_hours"])
            for row in matured_rows
            if row.get("horizon_hours") is not None
        }
    )

    windows: dict[str, Any] = {}
    window_specs: list[tuple[str, int | None]] = [("all", None)]
    window_specs.extend((f"{days}d", days) for days in sorted(set(rolling_days)))

    for label, days in window_specs:
        selected = _window_rows(matured_rows, now=current_time, days=days)
        windows[label] = {
            "days": days,
            "matured_rows": len(selected),
            "horizons": {
                f"{horizon}h": _horizon_report(
                    selected,
                    horizon=horizon,
                    low_sample_threshold=low_sample_threshold,
                )
                for horizon in horizons
            },
        }

    return {
        "generated_at": current_time.isoformat(),
        "baseline_model": PERSISTENCE_MODEL,
        "ensemble_model": ENSEMBLE_MODEL,
        "low_sample_threshold": low_sample_threshold,
        "rolling_days": sorted(set(rolling_days)),
        "matured_rows": len(matured_rows),
        "horizons": [f"{horizon}h" for horizon in horizons],
        "windows": windows,
    }


def _pct(value: Any, *, scale: float = 1.0) -> str:
    number = _safe_float(value)
    if number is None:
        return "—"
    return f"{number * scale:.2f}%"


def _metric_table_rows(report: dict[str, Any], window: str) -> list[list[str]]:
    rows: list[list[str]] = []
    horizons = report["windows"][window]["horizons"]
    for horizon in report["horizons"]:
        models = horizons[horizon]["models"]
        for model_name in _sort_models(models):
            metrics = models[model_name]
            rows.append(
                [
                    horizon,
                    model_name,
                    str(metrics["samples"]),
                    _pct(metrics["mae_pct"]),
                    _pct(metrics["mean_signed_error_pct"]),
                    _pct(metrics["direction_accuracy"], scale=100.0),
                    _pct(metrics["q10_q90_coverage"], scale=100.0),
                    metrics["confidence_warning"] or "",
                ]
            )
    return rows


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Forecast performance dashboard",
        "",
        f"Generated: `{report['generated_at']}`",
        f"Matured forecast rows: **{report['matured_rows']}**",
        f"Low-sample warning threshold: **{report['low_sample_threshold']}**",
        "",
        "Persistence is the required baseline in every horizon. Missing or low-sample "
        "segments are explicitly flagged.",
        "",
    ]

    for window in report["windows"]:
        label = "All time" if window == "all" else f"Last {window}"
        lines.extend(
            [
                f"## {label}",
                "",
                "| Horizon | Model | Samples | MAE | Bias | Direction | Q10–Q90 coverage | Warning |",
                "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        for values in _metric_table_rows(report, window):
            lines.append("| " + " | ".join(values) + " |")
        lines.append("")

        horizons = report["windows"][window]["horizons"]
        lines.append("### By market regime")
        lines.append("")
        for horizon in report["horizons"]:
            lines.append(f"#### {horizon}")
            lines.append("")
            lines.append(
                "| Regime | Model | Samples | MAE | Bias | Direction | Q10–Q90 coverage | Warning |"
            )
            lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |")
            for regime, models in horizons[horizon]["by_regime"].items():
                for model_name in _sort_models(models):
                    metrics = models[model_name]
                    lines.append(
                        "| "
                        + " | ".join(
                            [
                                regime,
                                model_name,
                                str(metrics["samples"]),
                                _pct(metrics["mae_pct"]),
                                _pct(metrics["mean_signed_error_pct"]),
                                _pct(metrics["direction_accuracy"], scale=100.0),
                                _pct(metrics["q10_q90_coverage"], scale=100.0),
                                metrics["confidence_warning"] or "",
                            ]
                        )
                        + " |"
                    )
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_html(report: dict[str, Any]) -> str:
    sections: list[str] = []
    for window in report["windows"]:
        label = "All time" if window == "all" else f"Last {window}"
        table_rows = []
        for values in _metric_table_rows(report, window):
            warning = values[-1]
            row_class = ' class="warn"' if warning else ""
            cells = "".join(f"<td>{html.escape(value)}</td>" for value in values)
            table_rows.append(f"<tr{row_class}>{cells}</tr>")

        regime_blocks: list[str] = []
        horizons = report["windows"][window]["horizons"]
        for horizon in report["horizons"]:
            regime_rows = []
            for regime, models in horizons[horizon]["by_regime"].items():
                for model_name in _sort_models(models):
                    metrics = models[model_name]
                    values = [
                        regime,
                        model_name,
                        str(metrics["samples"]),
                        _pct(metrics["mae_pct"]),
                        _pct(metrics["mean_signed_error_pct"]),
                        _pct(metrics["direction_accuracy"], scale=100.0),
                        _pct(metrics["q10_q90_coverage"], scale=100.0),
                        metrics["confidence_warning"] or "",
                    ]
                    row_class = ' class="warn"' if values[-1] else ""
                    cells = "".join(f"<td>{html.escape(value)}</td>" for value in values)
                    regime_rows.append(f"<tr{row_class}>{cells}</tr>")
            regime_blocks.append(
                f"""
                <details>
                  <summary>{html.escape(horizon)} by regime</summary>
                  <table>
                    <thead><tr><th>Regime</th><th>Model</th><th>Samples</th><th>MAE</th>
                    <th>Bias</th><th>Direction</th><th>Q10–Q90 coverage</th><th>Warning</th></tr></thead>
                    <tbody>{"".join(regime_rows)}</tbody>
                  </table>
                </details>
                """
            )

        sections.append(
            f"""
            <section>
              <h2>{html.escape(label)}</h2>
              <table>
                <thead><tr><th>Horizon</th><th>Model</th><th>Samples</th><th>MAE</th>
                <th>Bias</th><th>Direction</th><th>Q10–Q90 coverage</th><th>Warning</th></tr></thead>
                <tbody>{"".join(table_rows)}</tbody>
              </table>
              {"".join(regime_blocks)}
            </section>
            """
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BTC forecast performance dashboard</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; line-height: 1.4; color: #222; }}
table {{ border-collapse: collapse; width: 100%; margin: 1rem 0 2rem; }}
th, td {{ border: 1px solid #ddd; padding: .45rem .6rem; text-align: right; }}
th:first-child, td:first-child, th:nth-child(2), td:nth-child(2), th:last-child, td:last-child {{
  text-align: left;
}}
th {{ background: #f5f5f5; }}
.warn {{ background: #fff7d6; }}
details {{ margin: .75rem 0; }}
code {{ background: #f5f5f5; padding: .1rem .3rem; }}
</style>
</head>
<body>
<h1>BTC forecast performance dashboard</h1>
<p>Generated <code>{html.escape(report["generated_at"])}</code>. Matured rows:
<strong>{report["matured_rows"]}</strong>. Low-sample threshold:
<strong>{report["low_sample_threshold"]}</strong>.</p>
<p>Persistence is always shown as the baseline. Yellow rows indicate missing or low-sample segments.</p>
{"".join(sections)}
</body>
</html>
"""


def generate_dashboard(
    db_path: Path,
    *,
    json_path: Path,
    markdown_path: Path,
    html_path: Path,
    rolling_days: tuple[int, ...] = DEFAULT_ROLLING_DAYS,
    low_sample_threshold: int = DEFAULT_LOW_SAMPLE_THRESHOLD,
) -> dict[str, Any]:
    store = ForecastHistoryStore(db_path)
    verification = store.verify()
    verification_ok = (
        verification.get("integrity") == "ok"
        and int(verification.get("foreign_key_violations", 1)) == 0
        and verification.get("schema_version") == verification.get("supported_schema_version")
    )
    verification = {**verification, "ok": verification_ok}
    if not verification_ok:
        raise RuntimeError(f"forecast history failed verification: {verification}")

    report = build_report(
        store.export_rows(),
        rolling_days=rolling_days,
        low_sample_threshold=low_sample_threshold,
    )
    report["database_verification"] = verification

    for path in (json_path, markdown_path, html_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")
    return report


def _parse_rolling_days(value: str) -> tuple[int, ...]:
    try:
        days = tuple(sorted({int(item.strip()) for item in value.split(",") if item.strip()}))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("rolling days must be comma-separated integers") from exc
    if not days or any(day < 1 for day in days):
        raise argparse.ArgumentTypeError("rolling days must contain positive integers")
    return days


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate performance dashboard artifacts from durable forecast history"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--json", type=Path, default=Path("performance_dashboard.json"))
    parser.add_argument("--markdown", type=Path, default=Path("performance_dashboard.md"))
    parser.add_argument("--html", type=Path, default=Path("performance_dashboard.html"))
    parser.add_argument(
        "--rolling-days",
        type=_parse_rolling_days,
        default=DEFAULT_ROLLING_DAYS,
        help="Comma-separated rolling windows, default: 7,30,90",
    )
    parser.add_argument("--low-sample-threshold", type=int, default=DEFAULT_LOW_SAMPLE_THRESHOLD)
    args = parser.parse_args()

    report = generate_dashboard(
        args.db,
        json_path=args.json,
        markdown_path=args.markdown,
        html_path=args.html,
        rolling_days=args.rolling_days,
        low_sample_threshold=args.low_sample_threshold,
    )
    print(
        json.dumps(
            {
                "generated_at": report["generated_at"],
                "matured_rows": report["matured_rows"],
                "horizons": report["horizons"],
                "json": str(args.json),
                "markdown": str(args.markdown),
                "html": str(args.html),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
