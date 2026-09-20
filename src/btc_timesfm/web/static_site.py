#!/usr/bin/env python3
"""Generate a static GitHub Pages dashboard from durable forecast history."""

from __future__ import annotations

import argparse
import html
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

from btc_timesfm.history.history_store import DEFAULT_DB_PATH, ENSEMBLE_MODEL, ForecastHistoryStore
from btc_timesfm.web.charts import render_charts
from btc_timesfm.research.edge_attribution_report import build_report as build_edge_report
from btc_timesfm.research.performance_dashboard import build_report
from btc_timesfm.web.historical_explorer import build_explorer_data, render_explorer

DEFAULT_OUTPUT_DIR = Path("site")
DEFAULT_RECENT_ROWS = 80
MAX_SITE_BYTES = 5 * 1024 * 1024


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp must be a non-empty string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _money(value: Any) -> str:
    number = _safe_float(value)
    return "—" if number is None else f"${number:,.0f}"


def _pct(value: Any, digits: int = 2) -> str:
    number = _safe_float(value)
    return "—" if number is None else f"{number:.{digits}f}%"


def _ratio_pct(value: Any, digits: int = 1) -> str:
    number = _safe_float(value)
    return "—" if number is None else f"{number * 100:.{digits}f}%"


def _ensemble_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if str(row.get("model_name")) == ENSEMBLE_MODEL]


def _latest_predictions(
    rows: list[dict[str, Any]], latest_snapshot: dict[str, Any] | None
) -> dict[str, Any] | None:
    ensemble = _ensemble_rows(rows)
    if not ensemble:
        return None
    latest_origin = max(str(row["origin_at"]) for row in ensemble)
    selected = [row for row in ensemble if str(row["origin_at"]) == latest_origin]
    selected.sort(key=lambda row: int(row["horizon_hours"]))
    first = selected[0]

    snapshot = latest_snapshot if isinstance(latest_snapshot, dict) else {}
    direction_horizons = snapshot.get("direction_probability", {}).get("horizons", {})
    threshold_horizons = snapshot.get("dynamic_thresholds", {}).get("horizons", {})

    return {
        "origin_at": latest_origin,
        "source_price_usd": float(first["source_price_usd"]),
        "source_name": first.get("source_name"),
        "regime": first.get("regime"),
        "abstention": snapshot.get("abstention_policy"),
        "attribution": snapshot.get("attribution"),
        "predictions": [
            {
                "horizon_hours": int(row["horizon_hours"]),
                "target_at": row["target_at"],
                "predicted_price_usd": float(row["predicted_price_usd"]),
                "predicted_change_pct": float(row["predicted_change_pct"]),
                "q10_usd": _safe_float(row.get("q10_usd")),
                "q50_usd": _safe_float(row.get("q50_usd")),
                "q90_usd": _safe_float(row.get("q90_usd")),
                "actual_target_price_usd": _safe_float(row.get("actual_target_price_usd")),
                "absolute_error_pct": _safe_float(row.get("absolute_error_pct")),
                "direction_correct": row.get("direction_correct"),
                "direction_probability": direction_horizons.get(f"{int(row['horizon_hours'])}h"),
                "thresholds": threshold_horizons.get(f"{int(row['horizon_hours'])}h"),
            }
            for row in selected
        ],
    }


def _accuracy_summary(report: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for window in ("7d", "30d", "90d", "all"):
        if window not in report["windows"]:
            continue
        horizons: dict[str, Any] = {}
        for horizon in report["horizons"]:
            metrics = report["windows"][window]["horizons"][horizon]["models"].get(
                ENSEMBLE_MODEL, {}
            )
            horizons[horizon] = {
                "samples": int(metrics.get("samples") or 0),
                "mae_pct": _safe_float(metrics.get("mae_pct")),
                "mean_signed_error_pct": _safe_float(metrics.get("mean_signed_error_pct")),
                "direction_accuracy": _safe_float(metrics.get("direction_accuracy")),
                "q10_q90_coverage": _safe_float(metrics.get("q10_q90_coverage")),
                "confidence_warning": metrics.get("confidence_warning"),
            }
        summary[window] = horizons
    return summary


def _edge_summary(
    rows: list[dict[str, Any]], *, now: datetime, low_sample_threshold: int
) -> dict[str, Any]:
    windows: dict[str, Any] = {}
    for label, days in (("7d", 7), ("30d", 30), ("90d", 90), ("all", None)):
        selected = rows
        if days is not None:
            cutoff = now - timedelta(days=days)
            selected = [
                row
                for row in rows
                if row.get("origin_at") and _parse_timestamp(row["origin_at"]) >= cutoff
            ]
        report = build_edge_report(
            selected,
            now=now,
            low_sample_threshold=low_sample_threshold,
            bootstrap_iterations=1000,
        )
        windows[label] = {
            "days": days,
            "matured_rows": report["matured_rows"],
            "paired_samples": report["paired_samples"],
            "by_horizon": report["by_dimension"]["horizon"],
            "by_regime": report["by_dimension"]["regime"],
            "by_volatility_bucket": report["by_dimension"]["volatility_bucket"],
        }
    return {
        "low_sample_threshold": low_sample_threshold,
        "horizons": report["horizons"],
        "windows": windows,
        "reproducibility": report["reproducibility"],
    }


def build_site_data(
    rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    recent_limit: int = DEFAULT_RECENT_ROWS,
    latest_snapshot: dict[str, Any] | None = None,
    configuration_roles: dict[str, str] | None = None,
) -> dict[str, Any]:
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    report = build_report(rows, now=current_time)
    ensemble = sorted(
        _ensemble_rows(rows),
        key=lambda row: (str(row["origin_at"]), int(row["horizon_hours"])),
        reverse=True,
    )
    recent = []
    for row in ensemble[: max(0, recent_limit)]:
        recent.append(
            {
                "origin_at": row["origin_at"],
                "horizon_hours": int(row["horizon_hours"]),
                "target_at": row["target_at"],
                "source_price_usd": float(row["source_price_usd"]),
                "predicted_price_usd": float(row["predicted_price_usd"]),
                "predicted_change_pct": float(row["predicted_change_pct"]),
                "actual_target_price_usd": _safe_float(row.get("actual_target_price_usd")),
                "absolute_error_pct": _safe_float(row.get("absolute_error_pct")),
                "actual_change_pct": _safe_float(row.get("actual_change_pct")),
                "direction_correct": row.get("direction_correct"),
                "regime": row.get("regime"),
            }
        )

    chart_rows: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("model_name")) not in (ENSEMBLE_MODEL, "persistence"):
            continue
        chart_rows.append(
            {
                "model_name": row.get("model_name"),
                "origin_at": row.get("origin_at"),
                "horizon_hours": row.get("horizon_hours"),
                "q10_usd": _safe_float(row.get("q10_usd")),
                "q50_usd": _safe_float(row.get("q50_usd")),
                "q90_usd": _safe_float(row.get("q90_usd")),
                "actual_target_price_usd": _safe_float(row.get("actual_target_price_usd")),
                "absolute_error_pct": _safe_float(row.get("absolute_error_pct")),
                "direction_correct": row.get("direction_correct"),
                "within_q10_q90": row.get("within_q10_q90"),
                "regime": row.get("regime"),
            }
        )

    latest = _latest_predictions(rows, latest_snapshot)
    latest_age_hours: float | None = None
    if latest is not None:
        latest_age_hours = max(
            0.0,
            (current_time - _parse_timestamp(latest["origin_at"])).total_seconds() / 3600.0,
        )

    return {
        "schema_version": 1,
        "generated_at": current_time.isoformat(),
        "latest": latest,
        "latest_age_hours": round(latest_age_hours, 2) if latest_age_hours is not None else None,
        "accuracy": _accuracy_summary(report),
        "persistence_edge": _edge_summary(
            rows, now=current_time, low_sample_threshold=report["low_sample_threshold"]
        ),
        "recent": recent,
        "chart_rows": chart_rows,
        "explorer": {
            "version": 1,
            "horizons": sorted({int(row["horizon_hours"]) for row in ensemble}),
            "rows": [_explorer_row(row) for row in ensemble],
        },
        "historical_explorer": build_explorer_data(
            rows, now=current_time, configuration_roles=configuration_roles
        ),
        "matured_rows": report["matured_rows"],
        "horizons": report["horizons"],
        "low_sample_threshold": report["low_sample_threshold"],
        "multi_horizon_coherence": (
            latest_snapshot.get("multi_horizon_coherence")
            if isinstance(latest_snapshot, dict)
            else None
        ),
    }


def _explorer_row(row: dict[str, Any]) -> dict[str, Any]:
    actual = _safe_float(row.get("actual_target_price_usd"))
    return {
        "origin_at": row["origin_at"],
        "horizon_hours": int(row["horizon_hours"]),
        "target_at": row["target_at"],
        "source_price_usd": float(row["source_price_usd"]),
        "predicted_price_usd": float(row["predicted_price_usd"]),
        "predicted_change_pct": float(row["predicted_change_pct"]),
        "q10_usd": _safe_float(row.get("q10_usd")),
        "q50_usd": _safe_float(row.get("q50_usd")),
        "q90_usd": _safe_float(row.get("q90_usd")),
        "actual_target_price_usd": actual,
        "actual_change_pct": _safe_float(row.get("actual_change_pct")),
        "absolute_error_pct": _safe_float(row.get("absolute_error_pct")),
        "direction_correct": row.get("direction_correct"),
        "regime": row.get("regime"),
        "status": "matured" if actual is not None else "pending",
    }


def explorer_url_state(query: str, horizons: list[int]) -> dict[str, str | int | None]:
    params = parse_qs(query.lstrip("?"), keep_blank_values=False)
    valid_horizons = {str(horizon) for horizon in horizons}
    horizon = params.get("horizon", [""])[0]
    search = params.get("q", [""])[0].strip()[:80]
    days = params.get("days", ["all"])[0]
    sort = params.get("sort", ["origin"])[0]
    origin = params.get("origin", [""])[0].strip()
    return {
        "horizon": int(horizon) if horizon in valid_horizons else None,
        "q": search,
        "days": days if days in {"7", "30", "90", "all"} else "all",
        "sort": sort if sort in {"origin", "horizon", "error"} else "origin",
        "origin": origin if _valid_origin(origin) else None,
    }


def _valid_origin(value: str) -> bool:
    try:
        _parse_timestamp(value)
    except ValueError:
        return False
    return True


def explorer_query(state: dict[str, str | int | None]) -> str:
    values = [
        (key, str(state[key]))
        for key in ("days", "horizon", "q", "sort", "origin")
        if state.get(key) not in (None, "", "all", "origin")
    ]
    return urlencode(values)


def filter_explorer_rows(
    rows: list[dict[str, Any]], state: dict[str, str | int | None], *, now: datetime
) -> list[dict[str, Any]]:
    cutoff_days = state["days"]
    cutoff = now - timedelta(days=int(str(cutoff_days))) if cutoff_days != "all" else None
    selected = [
        row
        for row in rows
        if (state["horizon"] is None or row["horizon_hours"] == state["horizon"])
        and (cutoff is None or _parse_timestamp(row["origin_at"]) >= cutoff)
        and (
            not state["q"]
            or str(state["q"]).lower()
            in " ".join(
                str(row.get(key) or "")
                for key in ("origin_at", "regime", "status", "horizon_hours")
            ).lower()
        )
    ]
    sort = state["sort"]
    if sort == "horizon":
        return sorted(
            selected, key=lambda row: (row["horizon_hours"], row["origin_at"]), reverse=True
        )
    if sort == "error":
        return sorted(
            selected,
            key=lambda row: (row["absolute_error_pct"] is None, row["absolute_error_pct"] or 0),
        )
    return sorted(selected, key=lambda row: (row["origin_at"], row["horizon_hours"]), reverse=True)


def _status_label(row: dict[str, Any]) -> tuple[str, str]:
    if row.get("actual_target_price_usd") is None:
        return "Pending", "pending"
    if row.get("direction_correct") in (1, True):
        return "Direction ✓", "good"
    return "Direction ✕", "bad"


def _render_latest(data: dict[str, Any]) -> str:
    latest = data.get("latest")
    if not isinstance(latest, dict):
        return '<div class="empty">No forecast history is available yet.</div>'

    cards: list[str] = []
    for item in latest["predictions"]:
        change = float(item["predicted_change_pct"])
        direction_class = "up" if change >= 0 else "down"
        interval = ""
        if item.get("q10_usd") is not None and item.get("q90_usd") is not None:
            interval = (
                f'<div class="sub">80% interval {_money(item["q10_usd"])} – '
                f"{_money(item['q90_usd'])}</div>"
            )
        prob = item.get("direction_probability")
        thresholds = item.get("thresholds")
        extras = []
        if (
            isinstance(prob, dict)
            and prob.get("p_up") is not None
            and prob.get("p_down") is not None
        ):
            extras.append(
                f'<div class="sub">P(up): {_ratio_pct(prob["p_up"], digits=0)} | '
                f"P(down): {_ratio_pct(prob['p_down'], digits=0)}</div>"
            )
        if isinstance(thresholds, dict) and thresholds.get("edge_status") == "no_edge":
            extras.append('<div class="sub">No measurable edge</div>')

        cards.append(
            f"""
            <article class="prediction-card {direction_class}">
              <div class="eyebrow">+{int(item["horizon_hours"])}h</div>
              <div class="prediction-price">{_money(item["predicted_price_usd"])}</div>
              <div class="change">{change:+.2f}%</div>
              <div class="sub">Target {html.escape(str(item["target_at"]))}</div>
              {interval}
              {"".join(extras)}
            </article>
            """
        )

    age = data.get("latest_age_hours")
    stale = isinstance(age, (int, float)) and float(age) > 4.0
    freshness = (
        f'<span class="badge {"warn" if stale else "ok"}">'
        f"{'STALE' if stale else 'LIVE'} · {float(age):.1f}h old</span>"
        if isinstance(age, (int, float))
        else ""
    )
    abstention = latest.get("abstention")
    state = abstention.get("state") if isinstance(abstention, dict) else "healthy"
    state_display = ""
    if state != "healthy":
        state_display = f'<div style="margin-top:8px"><span class="badge warn">{html.escape(str(state))}</span></div>'

    attribution = latest.get("attribution")
    attribution_display = ""
    if isinstance(attribution, dict) and attribution.get("explanation"):
        attribution_display = f'<div class="note"><strong>Attribution:</strong> {html.escape(str(attribution["explanation"]))}</div>'

    return f"""
    <div class="current-strip">
      <div>
        <div class="eyebrow">Latest completed BTC candle</div>
        <div class="spot-price">{_money(latest["source_price_usd"])}</div>
        <div class="sub">{html.escape(str(latest["origin_at"]))} · {html.escape(str(latest.get("regime") or "unknown"))}</div>
        {state_display}
      </div>
      {freshness}
    </div>
    <div class="prediction-grid">{"".join(cards)}</div>
    {attribution_display}
    """


def _render_accuracy(data: dict[str, Any]) -> str:
    blocks: list[str] = []
    labels = {"7d": "7 days", "30d": "30 days", "90d": "90 days", "all": "All time"}
    for window in ("7d", "30d", "90d", "all"):
        horizons = data["accuracy"].get(window, {})
        rows = []
        for horizon in data["horizons"]:
            metrics = horizons.get(horizon, {})
            warning = metrics.get("confidence_warning")
            rows.append(
                f"""
                <tr class="{"low-sample" if warning else ""}">
                  <td><strong>{html.escape(horizon)}</strong></td>
                  <td>{int(metrics.get("samples") or 0)}</td>
                  <td>{_pct(metrics.get("mae_pct"))}</td>
                  <td>{_ratio_pct(metrics.get("direction_accuracy"))}</td>
                  <td>{_ratio_pct(metrics.get("q10_q90_coverage"))}</td>
                </tr>
                """
            )
        blocks.append(
            f"""
            <details {"open" if window == "30d" else ""}>
              <summary>{labels[window]}</summary>
              <div class="table-wrap">
                <table>
                  <thead><tr><th>Horizon</th><th>Samples</th><th>MAE</th><th>Direction</th><th>80% coverage</th></tr></thead>
                  <tbody>{"".join(rows)}</tbody>
                </table>
              </div>
            </details>
            """
        )
    return "".join(blocks)


def _render_edge_metrics(metrics: dict[str, Any]) -> str:
    delta = _safe_float(metrics.get("mae_delta_pct_points"))
    ci = metrics.get("confidence_interval")
    lower = _safe_float(ci.get("lower")) if isinstance(ci, dict) else None
    upper = _safe_float(ci.get("upper")) if isinstance(ci, dict) else None
    sign = (
        "positive"
        if delta is not None and delta > 0
        else "negative"
        if delta is not None and delta < 0
        else ""
    )
    return (
        f"<td>{int(metrics.get('samples') or 0)}</td>"
        f'<td class="{sign}">{_pct(delta)}</td>'
        f"<td>[{_pct(lower)}, {_pct(upper)}]</td>"
        f"<td>{html.escape(str(metrics.get('conclusion') or 'inconclusive'))}</td>"
    )


def _render_edge_rows(segments: dict[str, Any]) -> str:
    rows: list[str] = []
    for segment, metrics in segments.items():
        if not isinstance(metrics, dict):
            continue
        warning = bool(metrics.get("unstable_or_low_sample"))
        status = "Inconclusive" if warning else ""
        rows.append(
            f'<tr class="{"low-sample" if warning else ""}">'
            f"<td><strong>{html.escape(str(segment))}</strong></td>"
            f"{_render_edge_metrics(metrics)}"
            f"<td>{html.escape(status)}</td></tr>"
        )
    return "".join(rows)


def _render_persistence_edge(data: dict[str, Any]) -> str:
    edge = data["persistence_edge"]
    threshold = int(edge["low_sample_threshold"])
    labels = {"7d": "7 days", "30d": "30 days", "90d": "90 days", "all": "All time"}
    windows = []
    for window in ("7d", "30d", "90d", "all"):
        summary = edge["windows"].get(window, {})
        sections = [("Per horizon", summary.get("by_horizon", {}))]
        for label, key in (("By regime", "by_regime"), ("By volatility", "by_volatility_bucket")):
            segments = summary.get(key, {})
            if segments:
                sections.append((label, segments))
        tables = []
        for label, segments in sections:
            tables.append(
                f"<h3>{html.escape(label)}</h3>"
                '<div class="table-wrap edge-table"><table><thead><tr>'
                "<th>Segment</th><th>Paired samples</th><th>MAE edge</th><th>95% CI</th>"
                "<th>Result</th><th>Evidence</th></tr></thead>"
                f"<tbody>{_render_edge_rows(segments)}</tbody></table></div>"
            )
        windows.append(
            f"<details {'open' if window == '30d' else ''}>"
            f"<summary>{labels[window]}</summary>{''.join(tables)}</details>"
        )
    return (
        "<p>Positive MAE edge means lower ensemble error than persistence. "
        f"Cells with fewer than {threshold} paired forecasts or an inconclusive confidence interval "
        "are marked inconclusive.</p>" + "".join(windows)
    )


def _render_recent(data: dict[str, Any]) -> str:
    rows: list[str] = []
    for item in data["recent"]:
        label, css = _status_label(item)
        actual = _money(item.get("actual_target_price_usd"))
        error = _pct(item.get("absolute_error_pct"))
        rows.append(
            f"""
            <tr>
              <td>{html.escape(str(item["origin_at"]))}</td>
              <td>+{int(item["horizon_hours"])}h</td>
              <td>{_money(item["source_price_usd"])}</td>
              <td>{_money(item["predicted_price_usd"])}</td>
              <td class="{"positive" if float(item["predicted_change_pct"]) >= 0 else "negative"}">{float(item["predicted_change_pct"]):+.2f}%</td>
              <td>{actual}</td>
              <td>{error}</td>
              <td><span class="status {css}">{label}</span></td>
            </tr>
            """
        )
    return f"""
    <div class="table-wrap recent-table">
      <table>
        <thead><tr><th>Origin</th><th>Horizon</th><th>BTC then</th><th>Prediction</th><th>Move</th><th>Actual</th><th>Error</th><th>Status</th></tr></thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
    </div>
    """


def _render_accuracy_table(data: dict[str, Any], window: str, label: str) -> str:
    horizons = data["accuracy"].get(window, {})
    rows = []
    for horizon in data["horizons"]:
        metrics = horizons.get(horizon, {})
        warning = metrics.get("confidence_warning")
        rows.append(
            f"""
            <tr class="{"low-sample" if warning else ""}">
              <td><strong>{html.escape(horizon)}</strong></td>
              <td>{int(metrics.get("samples") or 0)}</td>
              <td>{_pct(metrics.get("mae_pct"))}</td>
              <td>{_ratio_pct(metrics.get("direction_accuracy"))}</td>
              <td>{_ratio_pct(metrics.get("q10_q90_coverage"))}</td>
            </tr>
            """
        )
    return (
        f"<details {'open' if window == '30d' else ''}>"
        f"<summary>{html.escape(label)}</summary>"
        '<div class="table-wrap">'
        f"<table><caption>Forecast accuracy for {html.escape(label)}</caption>"
        '<thead><tr><th scope="col">Horizon</th><th scope="col">Samples</th>'
        '<th scope="col">MAE</th><th scope="col">Direction</th>'
        '<th scope="col">80% coverage</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div></details>"
    )


def _render_explorer(data: dict[str, Any]) -> str:
    explorer = data["explorer"]
    rows = explorer["rows"]
    horizons = explorer["horizons"]
    options = '<option value="">All horizons</option>' + "".join(
        f'<option value="{horizon}">+{horizon}h</option>' for horizon in horizons
    )
    table_rows = []
    details = []
    for index, row in enumerate(rows):
        label, css = _status_label(row)
        anchor = f"forecast-{index}"
        query = explorer_query({"origin": str(row["origin_at"])})
        table_rows.append(
            f'<tr data-origin="{html.escape(str(row["origin_at"]))}" '
            f'data-horizon="{row["horizon_hours"]}" data-status="{row["status"]}" '
            f'data-regime="{html.escape(str(row.get("regime") or ""))}">'
            f'<td><a href="?{query}#{anchor}">{html.escape(str(row["origin_at"]))}</a></td>'
            f"<td>+{row['horizon_hours']}h</td><td>{_money(row['predicted_price_usd'])}</td>"
            f"<td>{_money(row['actual_target_price_usd'])}</td><td>{_pct(row['absolute_error_pct'])}</td>"
            f'<td><span class="status {css}">{label}</span></td></tr>'
        )
        details.append(
            f'<details id="{anchor}" class="forecast-detail" data-origin="{html.escape(str(row["origin_at"]))}">'
            f"<summary>{html.escape(str(row['origin_at']))} · +{row['horizon_hours']}h · {label}</summary>"
            f"<dl><dt>BTC at origin</dt><dd>{_money(row['source_price_usd'])}</dd>"
            f"<dt>Forecast</dt><dd>{_money(row['predicted_price_usd'])} ({float(row['predicted_change_pct']):+.2f}%)</dd>"
            f"<dt>80% interval</dt><dd>{_money(row['q10_usd'])} – {_money(row['q90_usd'])}</dd>"
            f"<dt>Actual outcome</dt><dd>{_money(row['actual_target_price_usd'])} ({_pct(row['actual_change_pct'])})</dd>"
            f"<dt>Error / direction</dt><dd>{_pct(row['absolute_error_pct'])} / {label}</dd>"
            f"<dt>Target / regime</dt><dd>{html.escape(str(row['target_at']))} / {html.escape(str(row.get('regime') or 'unknown'))}</dd></dl></details>"
        )
    return f"""<div class="explorer-controls" aria-label="Forecast explorer filters">
<label>Range <select id="explorer-days"><option value="all">All time</option><option value="7">7 days</option><option value="30">30 days</option><option value="90">90 days</option></select></label>
<label>Horizon <select id="explorer-horizon">{options}</select></label>
<label>Search <input id="explorer-search" type="search" maxlength="80" placeholder="Origin, regime, status"></label>
<label>Sort <select id="explorer-sort"><option value="origin">Newest origin</option><option value="horizon">Horizon</option><option value="error">Lowest error</option></select></label>
</div><p id="explorer-count" aria-live="polite">{len(rows)} forecasts</p>
<div class="table-wrap recent-table"><table id="explorer-table"><thead><tr><th>Origin</th><th>Horizon</th><th>Forecast</th><th>Actual</th><th>Error</th><th>Status</th></tr></thead><tbody>{"".join(table_rows)}</tbody></table></div>
<div id="forecast-details">{"".join(details)}</div>"""


def _explorer_script() -> str:
    return """<script>(()=>{const $=id=>document.getElementById(id),table=$("explorer-table"),body=table?.tBodies[0],days=$("explorer-days"),horizon=$("explorer-horizon"),search=$("explorer-search"),sort=$("explorer-sort"),count=$("explorer-count");if(!body)return;const p=new URLSearchParams(location.search),valid=(el,v)=>el instanceof HTMLSelectElement&&[...el.options].some(o=>o.value===v);for(const [k,el] of [["days",days],["horizon",horizon],["q",search],["sort",sort]]){const v=p.get(k)||el.value;if(el===search||valid(el,v))el.value=v}const apply=()=>{const q=search.value.trim().toLowerCase(),d=days.value,h=horizon.value,s=sort.value,cut=d==="all"?0:Date.now()-Number(d)*864e5;let rows=[...body.rows];rows.forEach(r=>{const searchable=[r.textContent,r.dataset.regime,r.dataset.status].join(" ").toLowerCase(),ok=(!h||r.dataset.horizon===h)&&(!cut||Date.parse(r.dataset.origin)>=cut)&&(!q||searchable.includes(q));r.hidden=!ok});rows.sort((a,b)=>s==="horizon"?b.dataset.horizon-a.dataset.horizon:s==="error"?(parseFloat(a.cells[4].textContent)||Infinity)-(parseFloat(b.cells[4].textContent)||Infinity):Date.parse(b.dataset.origin)-Date.parse(a.dataset.origin)).forEach(r=>body.append(r));const state=new URLSearchParams;d!=="all"&&state.set("days",d);h&&state.set("horizon",h);q&&state.set("q",q);s!=="origin"&&state.set("sort",s);history.replaceState(void 0,"",location.pathname+(state.size?"?"+state:"")+location.hash);count.textContent=rows.filter(r=>!r.hidden).length+" forecasts"};[days,horizon,search,sort].forEach(el=>el.addEventListener("input",apply));apply();const origin=p.get("origin");if(origin){const detail=[...document.querySelectorAll(".forecast-detail")].find(d=>d.dataset.origin===origin);if(detail){detail.open=true;detail.scrollIntoView()}}})();</script>"""


def render_html(data: dict[str, Any]) -> str:
    charts, chart_summary = render_charts(
        list(data.get("chart_rows", [])), list(data["horizons"]), int(data["low_sample_threshold"])
    )
    data["chart_summary"] = chart_summary
    data["chart_rows"] = []
    accuracy_tables = "".join(
        _render_accuracy_table(data, window, label)
        for window, label in (
            ("7d", "7 days"),
            ("30d", "30 days"),
            ("90d", "90 days"),
            ("all", "All time"),
        )
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>BTC TimesFM Forecasts</title>
<style>
:root {{ color-scheme: dark; --bg:#0a0c10; --panel:#12161d; --line:#242b36; --muted:#8f9aaa; --text:#f3f6fa; --green:#31d17c; --red:#ff646f; --amber:#f4c95d; --blue:#75a7ff; }}
[data-theme="light"] {{ --bg:#f8f9fb; --panel:#ffffff; --line:#dce1e8; --muted:#5a6577; --text:#1a1f27; --green:#1a9c56; --red:#cc3340; --amber:#b08a1e; --blue:#2563eb; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:radial-gradient(circle at top,#151b26 0,#0a0c10 40%); color:var(--text); line-height:1.5; }}
[data-theme="light"] body {{ background:#f8f9fb; }}
.skip-link {{ position:absolute; top:-40px; left:0; background:var(--blue); color:#fff; padding:8px 16px; z-index:100; font-weight:700; text-decoration:none; border-radius:0 0 6px 0; }}
.skip-link:focus {{ top:0; }}
*:focus-visible {{ outline:2px solid var(--blue); outline-offset:2px; }}
.tabs {{ display:flex; flex-wrap:wrap; gap:4px; margin:0 0 24px; border-bottom:1px solid var(--line); }}
.tab-button {{ background:transparent; border:0; border-bottom:2px solid transparent; color:var(--muted); cursor:pointer; font:inherit; font-size:.9rem; font-weight:700; padding:12px 16px; }}
.tab-button:hover, .tab-button:focus {{ color:var(--text); }}
.tab-button[aria-selected="true"] {{ border-color:var(--blue); color:var(--text); }}
.tab-content[hidden] {{ display:none; }}
.theme-toggle {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:6px 12px; cursor:pointer; color:var(--text); font-size:.8rem; font-weight:600; }}
.visually-hidden {{ position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }}
main {{ width:min(1180px,calc(100% - 32px)); margin:0 auto; padding:42px 0 72px; }}
header {{ display:flex; justify-content:space-between; gap:24px; align-items:flex-end; margin-bottom:34px; }}
h1 {{ font-size:clamp(2rem,5vw,4rem); line-height:1; margin:.25rem 0 .7rem; letter-spacing:-.045em; }}
h2 {{ margin:44px 0 14px; font-size:1.35rem; }}
h3 {{ margin:22px 0 8px; font-size:1rem; }}
p {{ color:var(--muted); }}
a {{ color:var(--blue); }}
.eyebrow {{ text-transform:uppercase; letter-spacing:.14em; font-size:.72rem; color:var(--muted); font-weight:700; }}
.sub {{ color:var(--muted); font-size:.82rem; }}
.current-strip {{ display:flex; justify-content:space-between; align-items:center; gap:20px; padding:24px; background:rgba(18,22,29,.88); border:1px solid var(--line); border-radius:18px; }}
.spot-price {{ font-size:2.25rem; font-weight:760; letter-spacing:-.04em; margin:.2rem 0; }}
.badge {{ border:1px solid var(--line); border-radius:99px; padding:7px 11px; font-size:.72rem; font-weight:800; letter-spacing:.08em; }}
.badge.ok {{ color:var(--green); border-color:rgba(49,209,124,.35); background:rgba(49,209,124,.08); }}
.badge.warn {{ color:var(--amber); border-color:rgba(244,201,93,.35); background:rgba(244,201,93,.08); }}
.prediction-grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px; margin-top:14px; }}
.prediction-card {{ background:var(--panel); border:1px solid var(--line); border-radius:16px; padding:20px; position:relative; overflow:hidden; }}
.prediction-card::before {{ content:""; position:absolute; inset:0 auto 0 0; width:3px; background:var(--green); }}
.prediction-card.down::before {{ background:var(--red); }}
.prediction-price {{ font-size:1.55rem; font-weight:740; letter-spacing:-.03em; margin:.45rem 0 .05rem; }}
.change {{ font-size:1rem; font-weight:750; color:var(--green); margin-bottom:.6rem; }}
.down .change {{ color:var(--red); }}
details {{ background:var(--panel); border:1px solid var(--line); border-radius:14px; margin:10px 0; overflow:hidden; }}
summary {{ padding:16px 18px; cursor:pointer; font-weight:700; }}
.table-wrap {{ overflow:auto; border-top:1px solid var(--line); }}
table {{ width:100%; border-collapse:collapse; min-width:650px; }}
th,td {{ padding:12px 14px; border-bottom:1px solid var(--line); text-align:right; white-space:nowrap; }}
th:first-child,td:first-child {{ text-align:left; }}
th {{ color:var(--muted); font-size:.72rem; letter-spacing:.05em; text-transform:uppercase; }}
td {{ font-size:.9rem; }}
.low-sample {{ background:rgba(244,201,93,.04); }}
.positive {{ color:var(--green); }} .negative {{ color:var(--red); }}
.status {{ padding:4px 8px; border-radius:99px; font-size:.72rem; font-weight:750; }}
.status.pending {{ background:rgba(117,167,255,.1); color:var(--blue); }}
.status.good {{ background:rgba(49,209,124,.1); color:var(--green); }}
.status.bad {{ background:rgba(255,100,111,.1); color:var(--red); }}
.recent-table {{ border:1px solid var(--line); border-radius:14px; background:var(--panel); }}
.explorer-controls {{ display:flex; flex-wrap:wrap; gap:12px; margin:12px 0; }}
.explorer-controls label {{ color:var(--muted); font-size:.85rem; display:grid; gap:4px; }}
select,input {{ background:var(--panel); border:1px solid var(--line); border-radius:7px; color:var(--text); padding:8px; font:inherit; }}
select:focus,input:focus,summary:focus,a:focus {{ outline:3px solid var(--blue); outline-offset:2px; }}
dl {{ display:grid; grid-template-columns:max-content 1fr; gap:8px 18px; padding:0 18px 18px; }} dt {{ color:var(--muted); }} dd {{ margin:0; }}
.explorer-table {{ border:1px solid var(--line); border-radius:14px; background:var(--panel); }}
.explorer-table small {{ color:var(--muted); font-size:.75rem; }}
.empty {{ padding:28px; border:1px dashed var(--line); border-radius:14px; color:var(--muted); }}
.note {{ margin-top:26px; padding:16px 18px; border-left:3px solid var(--blue); background:rgba(117,167,255,.06); color:var(--muted); }}
footer {{ margin-top:44px; color:var(--muted); font-size:.8rem; }}
.chart-panel {{ padding:0 0 14px; }} .chart {{ display:block; width:100%; height:auto; background:var(--panel); border-top:1px solid var(--line); }}
.chart-grid {{ stroke:var(--line); }} .chart-label,.chart-muted {{ fill:var(--muted); font-size:12px; }} .fan-band {{ fill:rgba(117,167,255,.22); }} .fan-median {{ fill:none; stroke:var(--blue); stroke-width:2; }} .fan-actual {{ fill:none; stroke:var(--text); stroke-width:1.5; }} .chart-low-shading {{ fill:rgba(244,201,93,.12); }} .chart-low-sample {{ fill:var(--amber); }}
@media (prefers-color-scheme: light) {{ :root {{ --bg:#f8fafc; --panel:#fff; --line:#d8dee8; --muted:#52606d; --text:#16202a; }} body {{ background:radial-gradient(circle at top,#e9f0fb 0,#f8fafc 40%); }} }}
@media (max-width:850px) {{ .prediction-grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} header {{ align-items:flex-start; flex-direction:column; }} }}
@media (max-width:520px) {{ main {{ width:min(100% - 20px,1180px); padding-top:24px; }} .prediction-grid {{ grid-template-columns:1fr; }} .current-strip {{ align-items:flex-start; flex-direction:column; }} }}
</style>
</head>
<body>
<a href="#content" class="skip-link">Skip to content</a>
<main id="content" role="main">
<header>
  <div>
    <div class="eyebrow">BTC TimesFM 3 · public forecast ledger</div>
    <h1>Forecasts & accuracy</h1>
    <p>Static, reproducible results generated from the durable production forecast history. No X/Twitter dependency.</p>
  </div>
  <div style="display:flex;gap:12px;align-items:center">
    <button class="theme-toggle" type="button" aria-label="Toggle light/dark theme" onclick="var t=document.documentElement;var c=t.getAttribute('data-theme');t.setAttribute('data-theme',c==='dark'?'light':c==='light'?'dark':(window.matchMedia&&window.matchMedia('(prefers-color-scheme:light)').matches?'dark':'light'))">Toggle theme</button>
    <a href="https://github.com/jpfelgueiras/btc-timesfm">View source on GitHub ↗</a>
  </div>
</header>
<div class="tabs" role="tablist" aria-label="Dashboard sections">
  <button class="tab-button" type="button" role="tab" aria-selected="true" aria-controls="tab-overview" id="tab-overview-button">Overview</button>
  <button class="tab-button" type="button" role="tab" aria-selected="false" aria-controls="tab-explorer" id="tab-explorer-button" tabindex="-1">Forecast Explorer</button>
  <button class="tab-button" type="button" role="tab" aria-selected="false" aria-controls="tab-metrics" id="tab-metrics-button" tabindex="-1">Model Metrics</button>
</div>
<div id="tab-overview" class="tab-content" role="tabpanel" aria-labelledby="tab-overview-button" tabindex="0">
<section id="forecasts" aria-labelledby="forecasts-heading">
  <h2 id="forecasts-heading" class="visually-hidden">Forecasts</h2>
  {_render_latest(data)}
</section>
<section id="about" aria-labelledby="about-heading">
  <h2 id="about-heading" class="visually-hidden">About</h2>
  <div class="note">Experimental forecasting only — not financial advice. Historical accuracy does not guarantee future performance.</div>
</section>
</div>
<div id="tab-metrics" class="tab-content" role="tabpanel" aria-labelledby="tab-metrics-button" tabindex="0" hidden>
<section id="accuracy" aria-labelledby="accuracy-heading">
  <h2 id="accuracy-heading">Accuracy</h2>
  <p>MAE is mean absolute percentage error. Direction is the share of forecasts that got the BTC move direction right. 80% coverage shows how often the actual price landed inside the q10–q90 interval.</p>
  {accuracy_tables}
</section>
<section id="charts" aria-labelledby="charts-heading">
  <h2 id="charts-heading">Quantile fans & performance trends</h2>
  <p>Server-generated charts show matured forecasts only. They include accessible text and a summary table, with no browser-side data processing.</p>
  {charts}
</section>
<section id="edge" aria-labelledby="edge-heading">
  <h2 id="edge-heading">Ensemble edge vs persistence</h2>
  {_render_persistence_edge(data)}
</section>
</div>
<div id="tab-explorer" class="tab-content" role="tabpanel" aria-labelledby="tab-explorer-button" tabindex="0" hidden>
<section id="explorer" aria-labelledby="explorer-heading">
  <h2 id="explorer-heading">Forecast explorer</h2>
  <p>Browse the durable ledger. Pending rows have not reached their target candle; matured rows are immutable historical predictions compared with actual BTC prices.</p>
  {_render_explorer(data)}
</section>
{render_explorer(data.get("historical_explorer", {}))}
<section id="recent" aria-labelledby="recent-heading">
  <h2 id="recent-heading">Recent forecast ledger</h2>
  <p>Pending rows have not reached their target candle yet. Matured rows are immutable historical predictions compared with the actual BTC price.</p>
  {_render_recent(data)}
</section>
</div>
<footer role="contentinfo">Generated {html.escape(str(data["generated_at"]))} from {int(data["matured_rows"])} matured forecast rows.</footer>
</main>
{_explorer_script()}
<script>
(function(){{const tabs=[...document.querySelectorAll('[role="tab"]')],panels=[...document.querySelectorAll('[role="tabpanel"]')],hashes={{'#explorer':'tab-explorer','#accuracy':'tab-metrics','#edge':'tab-metrics'}};function activate(id,replace){{tabs.forEach(tab=>{{const active=tab.getAttribute('aria-controls')===id;tab.setAttribute('aria-selected',String(active));tab.tabIndex=active?0:-1;}});panels.forEach(panel=>panel.hidden=panel.id!==id);if(replace)history.replaceState(null,'',id==='tab-overview'?location.pathname:'#'+(id==='tab-explorer'?'explorer':'accuracy'));}}tabs.forEach((tab,index)=>{{tab.addEventListener('click',()=>activate(tab.getAttribute('aria-controls'),true));tab.addEventListener('keydown',event=>{{const offsets={{ArrowRight:1,ArrowDown:1,ArrowLeft:-1,ArrowUp:-1}};const next=event.key==='Home'?0:event.key==='End'?tabs.length-1:offsets[event.key]===undefined?null:(index+offsets[event.key]+tabs.length)%tabs.length;if(next===null)return;event.preventDefault();tabs[next].focus();activate(tabs[next].getAttribute('aria-controls'),true);}});}});activate(hashes[location.hash]||'tab-overview',false);}})();
(function(){{{chr(123)}}}var t=document.documentElement;var m=window.matchMedia&&window.matchMedia('(prefers-color-scheme:dark)');if(m&&!t.getAttribute('data-theme')){chr(123)}t.setAttribute('data-theme',m.matches?'dark':'light');{chr(125)}{chr(125)})()
</script>
</body>
</html>
"""


def generate_site(
    db_path: Path,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    *,
    recent_limit: int = DEFAULT_RECENT_ROWS,
) -> dict[str, Any]:
    store = ForecastHistoryStore(db_path)
    verification = store.verify()
    if (
        verification.get("integrity") != "ok"
        or int(verification.get("foreign_key_violations", 1)) != 0
        or verification.get("schema_version") != verification.get("supported_schema_version")
    ):
        raise RuntimeError(f"forecast history failed verification: {verification}")

    snapshots = store.load_snapshots(limit=1)
    latest_snapshot = snapshots[0] if snapshots else None
    data = build_site_data(
        store.export_rows(),
        recent_limit=recent_limit,
        latest_snapshot=latest_snapshot,
    )
    data["database_verification"] = verification
    page = render_html(data)
    json_data = json.dumps(data, indent=2, sort_keys=True) + "\n"
    if len(page.encode("utf-8")) + len(json_data.encode("utf-8")) > MAX_SITE_BYTES:
        raise RuntimeError(f"site exceeds {MAX_SITE_BYTES} byte budget")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "index.html").write_text(page, encoding="utf-8")
    (output_dir / "data.json").write_text(json_data, encoding="utf-8")
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate the static BTC forecast GitHub Pages site"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--recent-limit", type=int, default=DEFAULT_RECENT_ROWS)
    args = parser.parse_args()
    data = generate_site(args.db, args.output_dir, recent_limit=args.recent_limit)
    latest = data.get("latest") or {}
    print(
        json.dumps(
            {
                "output_dir": str(args.output_dir),
                "generated_at": data["generated_at"],
                "latest_origin_at": latest.get("origin_at"),
                "matured_rows": data["matured_rows"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
