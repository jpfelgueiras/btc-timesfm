#!/usr/bin/env python3
"""Generate a static GitHub Pages dashboard from durable forecast history."""

from __future__ import annotations

import argparse
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from btc_timesfm.history.history_store import DEFAULT_DB_PATH, ENSEMBLE_MODEL, ForecastHistoryStore
from btc_timesfm.research.performance_dashboard import build_report

DEFAULT_OUTPUT_DIR = Path("site")
DEFAULT_RECENT_ROWS = 80


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


def _latest_predictions(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    ensemble = _ensemble_rows(rows)
    if not ensemble:
        return None
    latest_origin = max(str(row["origin_at"]) for row in ensemble)
    selected = [row for row in ensemble if str(row["origin_at"]) == latest_origin]
    selected.sort(key=lambda row: int(row["horizon_hours"]))
    first = selected[0]
    return {
        "origin_at": latest_origin,
        "source_price_usd": float(first["source_price_usd"]),
        "source_name": first.get("source_name"),
        "regime": first.get("regime"),
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


def build_site_data(
    rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    recent_limit: int = DEFAULT_RECENT_ROWS,
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

    latest = _latest_predictions(rows)
    latest_age_hours: float | None = None
    if latest is not None:
        latest_age_hours = max(
            0.0,
            (current_time - _parse_timestamp(latest["origin_at"])).total_seconds() / 3600.0,
        )

    return {
        "generated_at": current_time.isoformat(),
        "latest": latest,
        "latest_age_hours": round(latest_age_hours, 2) if latest_age_hours is not None else None,
        "accuracy": _accuracy_summary(report),
        "recent": recent,
        "matured_rows": report["matured_rows"],
        "horizons": report["horizons"],
        "low_sample_threshold": report["low_sample_threshold"],
    }


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
                f'{_money(item["q90_usd"])}</div>'
            )
        cards.append(
            f"""
            <article class="prediction-card {direction_class}">
              <div class="eyebrow">+{int(item['horizon_hours'])}h</div>
              <div class="prediction-price">{_money(item['predicted_price_usd'])}</div>
              <div class="change">{change:+.2f}%</div>
              <div class="sub">Target {html.escape(str(item['target_at']))}</div>
              {interval}
            </article>
            """
        )

    age = data.get("latest_age_hours")
    stale = isinstance(age, (int, float)) and float(age) > 4.0
    freshness = (
        f'<span class="badge {"warn" if stale else "ok"}">'
        f'{"STALE" if stale else "LIVE"} · {float(age):.1f}h old</span>'
        if isinstance(age, (int, float))
        else ""
    )
    return f"""
    <div class="current-strip">
      <div>
        <div class="eyebrow">Latest completed BTC candle</div>
        <div class="spot-price">{_money(latest['source_price_usd'])}</div>
        <div class="sub">{html.escape(str(latest['origin_at']))} · {html.escape(str(latest.get('regime') or 'unknown'))}</div>
      </div>
      {freshness}
    </div>
    <div class="prediction-grid">{''.join(cards)}</div>
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
                <tr class="{'low-sample' if warning else ''}">
                  <td><strong>{html.escape(horizon)}</strong></td>
                  <td>{int(metrics.get('samples') or 0)}</td>
                  <td>{_pct(metrics.get('mae_pct'))}</td>
                  <td>{_ratio_pct(metrics.get('direction_accuracy'))}</td>
                  <td>{_ratio_pct(metrics.get('q10_q90_coverage'))}</td>
                </tr>
                """
            )
        blocks.append(
            f"""
            <details {'open' if window == '30d' else ''}>
              <summary>{labels[window]}</summary>
              <div class="table-wrap">
                <table>
                  <thead><tr><th>Horizon</th><th>Samples</th><th>MAE</th><th>Direction</th><th>80% coverage</th></tr></thead>
                  <tbody>{''.join(rows)}</tbody>
                </table>
              </div>
            </details>
            """
        )
    return "".join(blocks)


def _render_recent(data: dict[str, Any]) -> str:
    rows: list[str] = []
    for item in data["recent"]:
        label, css = _status_label(item)
        actual = _money(item.get("actual_target_price_usd"))
        error = _pct(item.get("absolute_error_pct"))
        rows.append(
            f"""
            <tr>
              <td>{html.escape(str(item['origin_at']))}</td>
              <td>+{int(item['horizon_hours'])}h</td>
              <td>{_money(item['source_price_usd'])}</td>
              <td>{_money(item['predicted_price_usd'])}</td>
              <td class="{'positive' if float(item['predicted_change_pct']) >= 0 else 'negative'}">{float(item['predicted_change_pct']):+.2f}%</td>
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
        <tbody>{''.join(rows)}</tbody>
      </table>
    </div>
    """


def render_html(data: dict[str, Any]) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>BTC TimesFM Forecasts</title>
<style>
:root {{ color-scheme: dark; --bg:#0a0c10; --panel:#12161d; --line:#242b36; --muted:#8f9aaa; --text:#f3f6fa; --green:#31d17c; --red:#ff646f; --amber:#f4c95d; --blue:#75a7ff; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:radial-gradient(circle at top,#151b26 0,#0a0c10 40%); color:var(--text); line-height:1.5; }}
main {{ width:min(1180px,calc(100% - 32px)); margin:0 auto; padding:42px 0 72px; }}
header {{ display:flex; justify-content:space-between; gap:24px; align-items:flex-end; margin-bottom:34px; }}
h1 {{ font-size:clamp(2rem,5vw,4rem); line-height:1; margin:.25rem 0 .7rem; letter-spacing:-.045em; }}
h2 {{ margin:44px 0 14px; font-size:1.35rem; }}
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
.empty {{ padding:28px; border:1px dashed var(--line); border-radius:14px; color:var(--muted); }}
.note {{ margin-top:26px; padding:16px 18px; border-left:3px solid var(--blue); background:rgba(117,167,255,.06); color:var(--muted); }}
footer {{ margin-top:44px; color:var(--muted); font-size:.8rem; }}
@media (max-width:850px) {{ .prediction-grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} header {{ align-items:flex-start; flex-direction:column; }} }}
@media (max-width:520px) {{ main {{ width:min(100% - 20px,1180px); padding-top:24px; }} .prediction-grid {{ grid-template-columns:1fr; }} .current-strip {{ align-items:flex-start; flex-direction:column; }} }}
</style>
</head>
<body>
<main>
<header>
  <div>
    <div class="eyebrow">BTC TimesFM 3 · public forecast ledger</div>
    <h1>Forecasts & accuracy</h1>
    <p>Static, reproducible results generated from the durable production forecast history. No X/Twitter dependency.</p>
  </div>
  <a href="https://github.com/jpfelgueiras/btc-timesfm">View source on GitHub ↗</a>
</header>
<section>
  {_render_latest(data)}
</section>
<section>
  <h2>Accuracy</h2>
  <p>MAE is mean absolute percentage error. Direction is the share of forecasts that got the BTC move direction right. 80% coverage shows how often the actual price landed inside the q10–q90 interval.</p>
  {_render_accuracy(data)}
</section>
<section>
  <h2>Recent forecast ledger</h2>
  <p>Pending rows have not reached their target candle yet. Matured rows are immutable historical predictions compared with the actual BTC price.</p>
  {_render_recent(data)}
</section>
<div class="note">Experimental forecasting only — not financial advice. Historical accuracy does not guarantee future performance.</div>
<footer>Generated {html.escape(str(data['generated_at']))} from {int(data['matured_rows'])} matured forecast rows.</footer>
</main>
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

    data = build_site_data(store.export_rows(), recent_limit=recent_limit)
    data["database_verification"] = verification
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "index.html").write_text(render_html(data), encoding="utf-8")
    (output_dir / "data.json").write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the static BTC forecast GitHub Pages site")
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
