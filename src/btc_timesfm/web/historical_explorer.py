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
    for item in forecasts:
        if not isinstance(item, Mapping):
            continue
        identity = item.get("identity")
        identity = identity if isinstance(identity, Mapping) else {}
        matured = item.get("maturity") == "matured"
        outcome = (
            f"{_money(item.get('actual_target_price_usd'))} / {_pct(item.get('absolute_error_pct'))}"
            if matured
            else "Pending maturity"
        )
        role = html.escape(str(identity.get("role") or "unclassified"))
        configuration = html.escape(str(identity.get("configuration_id") or "metadata unavailable"))
        run_id = html.escape(str(identity.get("run_id") or "metadata unavailable"))
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('origin_at') or ''))}</td>"
            f"<td>+{int(item.get('horizon_hours') or 0)}h</td>"
            f"<td>{html.escape(str(item.get('model_name') or 'unknown'))}</td>"
            f"<td>{role}<br><small>{configuration}<br>{run_id}</small></td>"
            f"<td>{_money(item.get('source_price_usd'))}</td>"
            f"<td>{_money(item.get('predicted_price_usd'))}<br><small>{_pct(item.get('predicted_change_pct'))}</small></td>"
            f"<td>{_money(item.get('q10_usd'))} – {_money(item.get('q90_usd'))}</td>"
            f"<td>{outcome}</td>"
            "</tr>"
        )
    body = "".join(rows) or '<tr><td colspan="8">No historical forecasts are available.</td></tr>'
    return (
        '<section id="historical-explorer"><h2>Historical forecast explorer</h2>'
        "<p>Audit original forecasts by origin date and horizon. Outcomes and errors appear only after "
        "their target time has matured. Configuration metadata identifies champion/challenger runs when available.</p>"
        '<div class="table-wrap explorer-table"><table><thead><tr>'
        "<th>Origin date</th><th>Horizon</th><th>Forecast model</th><th>Experiment identity</th>"
        "<th>BTC then</th><th>Original forecast</th><th>80% interval</th><th>Outcome / error</th>"
        f"</tr></thead><tbody>{body}</tbody></table></div></section>"
    )
