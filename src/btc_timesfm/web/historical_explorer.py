"""Build and render a static historical forecast explorer."""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _money(value: Any) -> str:
    number = _number(value)
    return "—" if number is None else f"${number:,.2f}"


def _pct(value: Any) -> str:
    number = _number(value)
    return "—" if number is None else f"{number:.2f}%"


def _manifest(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _identity(row: Mapping[str, Any], roles: Mapping[str, str]) -> dict[str, str]:
    manifest = _manifest(row.get("experiment_manifest_json"))
    configuration_id = str(row.get("configuration_id") or manifest.get("configuration_id") or "")
    run_id = str(row.get("experiment_run_id") or manifest.get("run_id") or "")
    role = roles.get(configuration_id)
    if role is None:
        raw_role = manifest.get("role") or manifest.get("deployment_role")
        role = str(raw_role) if raw_role else "champion (production)"
    return {
        "role": role,
        "configuration_id": configuration_id or "metadata unavailable",
        "run_id": run_id or "metadata unavailable",
    }


def build_explorer_data(
    rows: Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    configuration_roles: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Create an audit-safe export without exposing outcomes before maturity."""
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    roles = dict(configuration_roles or {})
    forecasts: list[dict[str, Any]] = []
    for row in rows:
        target_at = str(row.get("target_at") or "")
        target_time = _parse_timestamp(target_at)
        matured = target_time is not None and target_time <= current_time
        actual = _number(row.get("actual_target_price_usd")) if matured else None
        has_outcome = actual is not None
        forecasts.append(
            {
                "origin_at": str(row.get("origin_at") or ""),
                "date": str(row.get("origin_at") or "")[:10] or "unknown",
                "target_at": target_at,
                "horizon_hours": int(row.get("horizon_hours") or 0),
                "model_name": str(row.get("model_name") or "unknown"),
                "source_price_usd": _number(row.get("source_price_usd")),
                "predicted_price_usd": _number(row.get("predicted_price_usd")),
                "predicted_change_pct": _number(row.get("predicted_change_pct")),
                "q10_usd": _number(row.get("q10_usd")),
                "q50_usd": _number(row.get("q50_usd")),
                "q90_usd": _number(row.get("q90_usd")),
                "maturity": "matured" if has_outcome else "pending",
                "actual_target_price_usd": actual,
                "absolute_error_pct": _number(row.get("absolute_error_pct"))
                if has_outcome
                else None,
                "signed_error_pct": _number(row.get("signed_error_pct")) if has_outcome else None,
                "actual_change_pct": _number(row.get("actual_change_pct")) if has_outcome else None,
                "direction_correct": row.get("direction_correct") if has_outcome else None,
                "identity": _identity(row, roles),
            }
        )
    forecasts.sort(
        key=lambda item: (item["origin_at"], item["horizon_hours"], item["model_name"]),
        reverse=True,
    )
    dates = sorted({str(item["date"]) for item in forecasts}, reverse=True)
    horizons = sorted({int(item["horizon_hours"]) for item in forecasts})
    return {
        "generated_at": current_time.isoformat(),
        "dates": dates,
        "horizons": horizons,
        "forecasts": forecasts,
        "matured_forecasts": sum(item["maturity"] == "matured" for item in forecasts),
    }


def render_explorer(data: Mapping[str, Any]) -> str:
    forecasts = data.get("forecasts")
    if not isinstance(forecasts, list):
        forecasts = []
    rows: list[str] = []
    models: set[str] = set()
    horizons = sorted(
        {int(item.get("horizon_hours") or 0) for item in forecasts if isinstance(item, Mapping)}
    )
    for item in forecasts:
        if not isinstance(item, Mapping):
            continue
        identity = item.get("identity")
        identity = identity if isinstance(identity, Mapping) else {}
        matured = item.get("maturity") == "matured"
        outcome = (
            f"Matured: {_money(item.get('actual_target_price_usd'))} / "
            f"{_pct(item.get('absolute_error_pct'))}"
            if matured
            else "Pending maturity"
        )
        configuration = str(identity.get("configuration_id") or "metadata unavailable")
        run_id = str(identity.get("run_id") or "metadata unavailable")
        experiment_ids = (
            "configuration/run identifiers unavailable"
            if configuration == run_id == "metadata unavailable"
            else f"configuration {configuration}; run {run_id}"
        )
        experiment = html.escape(experiment_ids)
        model = str(item.get("model_name") or "unknown")
        models.add(model)
        display_role = html.escape(str(identity.get("role") or "unclassified"))
        origin = html.escape(str(item.get("origin_at") or "unknown"))
        target = html.escape(str(item.get("target_at") or "unknown"))
        rows.append(
            '<tr><td><details class="history-detail">'
            f"<summary>{origin}</summary>"
            f"<p>Target: {target} · q10–q90: {_money(item.get('q10_usd'))}–{_money(item.get('q90_usd'))} · Experiment: {experiment}</p></details></td>"
            f"<td>+{int(item.get('horizon_hours') or 0)}h · {html.escape(model)} · {display_role}</td>"
            f"<td>{_money(item.get('source_price_usd'))} → {_money(item.get('predicted_price_usd'))} "
            f"({_pct(item.get('predicted_change_pct'))})</td>"
            f"<td>{outcome}</td></tr>"
        )
    body = "".join(rows) or '<tr><td colspan="4">No historical forecasts are available.</td></tr>'
    horizon_options = "".join(
        f'<option value="{horizon}">+{horizon}h</option>' for horizon in horizons
    )
    model_options = "".join(
        f'<option value="{html.escape(model, quote=True)}">{html.escape(model)}</option>'
        for model in sorted(models)
    )
    return (
        '<section id="explorer" aria-labelledby="history-heading"><h2 id="history-heading">Forecast History Explorer</h2>'
        "<p>Browse the full forecast ledger across models. Pending outcomes remain hidden until target maturity; expand a row to inspect the original prediction, interval and available configuration identity.</p>"
        '<div class="explorer-controls" aria-label="Forecast history filters">'
        '<label>Date range <select id="history-days"><option value="all">All available history</option><option value="7">Last 7 days</option><option value="30">Last 30 days</option><option value="90">Last 90 days</option></select></label>'
        f'<label>Horizon <select id="history-horizon"><option value="">All horizons</option>{horizon_options}</select></label>'
        '<label>Maturity <select id="history-status"><option value="">All outcomes</option><option value="matured">Matured</option><option value="pending">Pending</option></select></label>'
        f'<label>Model <select id="history-model"><option value="">All models</option>{model_options}</select></label>'
        f'</div><p class="sub">Filters need JavaScript; with scripts off, the full generated forecast table remains available below.</p>'
        f'<p id="history-count" aria-live="polite">{len(forecasts)} of {len(forecasts)} forecasts</p>'
        '<div id="history-no-matches" class="empty" hidden>No forecasts match these filters. Expand the date range or reset the horizon, maturity, and model filters.</div>'
        '<div class="table-wrap explorer-table" role="region" aria-label="Scrollable forecast history table" tabindex="0"><table id="history-table"><thead><tr>'
        '<th scope="col">Forecast origin (UTC; expand details)</th><th scope="col">Horizon / model / role</th>'
        '<th scope="col">Source BTC / forecast</th><th scope="col">Maturity / actual / error</th>'
        f"</tr></thead><tbody>{body}</tbody></table></div>"
        "<noscript><p>Filters require JavaScript; the complete generated forecast table above remains available.</p></noscript>"
        "</section>"
        '<script>(()=>{const $=id=>document.getElementById(id),table=$("history-table"),rows=[...table.tBodies[0].rows],days=$("history-days"),horizon=$("history-horizon"),status=$("history-status"),model=$("history-model"),count=$("history-count"),empty=$("history-no-matches"),params=new URLSearchParams(location.search);'
        'const filters=[["days",days,"all"],["horizon",horizon,""],["status",status,""],["model",model,""]];'
        "for(const [key,el] of filters){const value=params.get(key);if(value&&[...el.options].some(option=>option.value===value))el.value=value;}"
        'const apply=(write=true)=>{const now=Date.now(),cutoff=days.value==="all"?0:now-Number(days.value)*864e5;let visible=0;for(const row of rows){const date=Date.parse(row.cells[0].querySelector("summary").textContent),fields=row.cells[1].textContent.split(" · "),ok=(!cutoff||date>=cutoff)&&(!horizon.value||fields[0].trim()==="+"+horizon.value+"h")&&(!status.value||row.cells[3].textContent.trim().toLowerCase().startsWith(status.value))&&(!model.value||fields[1].trim()===model.value);row.hidden=!ok;if(ok)visible++;}count.textContent=visible+" of "+rows.length+" forecasts";empty.hidden=visible>0;if(write){const next=new URLSearchParams;for(const [key,el,defaultValue] of filters)if(el.value!==defaultValue)next.set(key,el.value);history.pushState(void 0,"",location.pathname+(next.size?"?"+next:"" )+location.hash);}};'
        'for(const [,el] of filters)el.addEventListener("change",()=>apply());window.addEventListener("popstate",()=>{const current=new URLSearchParams(location.search);for(const [key,el,defaultValue] of filters)el.value=current.get(key)||defaultValue;apply(false);});apply(false);})();</script>'
    )
