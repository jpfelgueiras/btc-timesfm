"""Accessible, dependency-free SVG charts for the static forecast dashboard."""

from __future__ import annotations

import html
import math
from collections import defaultdict
from typing import Any, Iterable

CHART_WIDTH = 760
CHART_HEIGHT = 260
MAX_POINTS_PER_CHART = 80
MAX_CHART_PAYLOAD_BYTES = 180_000
ROLLING_WINDOW = 12
REGIME_COLORS = {
    "trending": "#7c9cff",
    "range": "#b28cff",
    "volatile": "#ff9f6e",
    "unknown": "#8f9aaa",
}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _sample(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(rows) <= MAX_POINTS_PER_CHART:
        return rows
    stride = (len(rows) - 1) / (MAX_POINTS_PER_CHART - 1)
    return [rows[round(index * stride)] for index in range(MAX_POINTS_PER_CHART)]


def _scale(values: Iterable[float], low: float, high: float) -> list[float]:
    values = list(values)
    if not values:
        return []
    minimum, maximum = min(values), max(values)
    if minimum == maximum:
        return [(low + high) / 2 for _ in values]
    return [max(low, min(high, low + (value - minimum) * (high - low) / (maximum - minimum))) for value in values]


def _points(xs: list[float], ys: list[float]) -> str:
    return " ".join(f"{x:.2f},{y:.2f}" for x, y in zip(xs, ys))


def _svg(title: str, description: str, body: str, *, height: int = CHART_HEIGHT) -> str:
    title_id = "chart-" + "".join(character if character.isalnum() else "-" for character in title)
    return (
        f'<svg class="chart" viewBox="0 0 {CHART_WIDTH} {height}" role="img" '
        f'aria-labelledby="{title_id}" xmlns="http://www.w3.org/2000/svg">'
        f"<title id=\"{title_id}\">{_escape(title)}</title>"
        f"<desc>{_escape(description)}</desc>{body}</svg>"
    )


def _empty(title: str, message: str) -> str:
    return _svg(title, message, f'<text x="24" y="42" class="chart-muted">{_escape(message)}</text>')


def _fan_chart(horizon: str, rows: list[dict[str, Any]]) -> str:
    usable = []
    for row in rows:
        q10, q50, q90, actual = (_number(row.get(key)) for key in ("q10_usd", "q50_usd", "q90_usd", "actual_target_price_usd"))
        if None not in (q10, q50, q90, actual) and q10 <= q50 <= q90:
            usable.append((row, q10, q50, q90, actual))
    usable = _sample(usable)
    title = f"{horizon} quantile fan chart"
    if not usable:
        return _empty(title, "No matured forecasts with q10, q50, q90, and actual prices are available.")
    values = [value for _, q10, q50, q90, actual in usable for value in (q10, q50, q90, actual)]
    ys = _scale(values, 224, 28)
    q10_y, q50_y, q90_y, actual_y = (ys[offset::4] for offset in range(4))
    xs = _scale(list(range(len(usable))), 36, CHART_WIDTH - 20)
    band = _points(xs + list(reversed(xs)), q10_y + list(reversed(q90_y)))
    actual = "".join(
        f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3" fill="{REGIME_COLORS.get(str(row.get("regime", "unknown")).lower(), REGIME_COLORS["unknown"])}">'
        f"<title>Actual; regime {_escape(row.get('regime') or 'unknown')}</title></circle>"
        for (row, *_), x, y in zip(usable, xs, actual_y)
    )
    minimum, maximum = min(values), max(values)
    body = (
        f'<line x1="36" y1="224" x2="740" y2="224" class="chart-grid"/>'
        f'<text x="4" y="32" class="chart-label">${maximum:,.0f}</text>'
        f'<text x="4" y="228" class="chart-label">${minimum:,.0f}</text>'
        f'<polygon points="{band}" class="fan-band"/>'
        f'<polyline points="{_points(xs, q50_y)}" class="fan-median"/>'
        f'<polyline points="{_points(xs, actual_y)}" class="fan-actual"/>{actual}'
        '<text x="38" y="250" class="chart-label">q10–q90 band · q50 line · actual dots colored by regime</text>'
    )
    return _svg(title, f"{len(usable)} matured {horizon} forecasts. Shaded band is q10 to q90.", body)


def _rolling(values: list[float | None], window: int = ROLLING_WINDOW) -> tuple[list[float | None], list[int]]:
    result: list[float | None] = []
    counts: list[int] = []
    for index in range(len(values)):
        selected = [value for value in values[max(0, index - window + 1) : index + 1] if value is not None]
        counts.append(len(selected))
        result.append(sum(selected) / len(selected) if selected else None)
    return result, counts


def _performance_chart(horizon: str, rows: list[dict[str, Any]], persistence: dict[tuple[str, int], float], threshold: int) -> str:
    rows = _sample(rows)
    title = f"{horizon} rolling performance"
    if not rows:
        return _empty(title, "No matured forecasts are available.")
    mae = [_number(row.get("absolute_error_pct")) for row in rows]
    direction = [1.0 if row.get("direction_correct") in (1, True) else 0.0 if row.get("direction_correct") in (0, False) else None for row in rows]
    coverage = [1.0 if row.get("within_q10_q90") in (1, True) else 0.0 if row.get("within_q10_q90") in (0, False) else None for row in rows]
    skill = [
        persistence.get((str(row.get("origin_at")), int(row.get("horizon_hours") or 0))) - error if error is not None and persistence.get((str(row.get("origin_at")), int(row.get("horizon_hours") or 0))) is not None else None
        for row, error in zip(rows, mae)
    ]
    series = [("MAE %", mae, "#75a7ff"), ("Direction %", direction, "#31d17c"), ("80% coverage", coverage, "#f4c95d"), ("Skill vs persistence pp", skill, "#d2a8ff")]
    xs = _scale(list(range(len(rows))), 120, CHART_WIDTH - 20)
    body: list[str] = []
    for panel, (label, values, color) in enumerate(series):
        rolling, counts = _rolling(values)
        top, bottom = 18 + panel * 56, 58 + panel * 56
        finite = [value for value in rolling if value is not None]
        if finite:
            scale_values = _scale(finite, bottom, top)
            mapped = iter(scale_values)
            points = [(x, next(mapped)) for x, value in zip(xs, rolling) if value is not None]
            body.append(f'<polyline points="{_points([x for x, _ in points], [y for _, y in points])}" fill="none" stroke="{color}" stroke-width="2"/>')
        body.append(f'<text x="4" y="{top + 16}" class="chart-label">{label}</text>')
        low_x = [x for x, count in zip(xs, counts) if count < threshold]
        if low_x:
            width = max(4.0, low_x[-1] - low_x[0] + 4.0)
            body.append(
                f'<rect x="{low_x[0] - 2:.2f}" y="{top:.2f}" width="{width:.2f}" '
                f'height="{bottom - top:.2f}" class="chart-low-shading"><title>Low sample count: fewer than {threshold}</title></rect>'
            )
            body.append(f'<circle cx="{low_x[-1]:.2f}" cy="{bottom:.2f}" r="3" class="chart-low-sample"/>')
    body.append('<text x="120" y="250" class="chart-label">Lines use a 12-forecast rolling window; dots mark low sample windows.</text>')
    return _svg(title, f"Rolling MAE, direction accuracy, 80% coverage, and skill versus persistence for {horizon}.", "".join(body))


def render_charts(rows: list[dict[str, Any]], horizons: list[str], low_sample_threshold: int) -> tuple[str, dict[str, Any]]:
    ensemble = [row for row in rows if str(row.get("model_name")) == "ensemble" and _number(row.get("actual_target_price_usd")) is not None]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sorted(ensemble, key=lambda item: str(item.get("origin_at", ""))):
        grouped[f"{int(row.get('horizon_hours') or 0)}h"].append(row)
    persistence = {
        (str(row.get("origin_at")), int(row.get("horizon_hours") or 0)): value
        for row in rows
        if str(row.get("model_name")) == "persistence"
        for value in [_number(row.get("absolute_error_pct"))]
        if value is not None
    }
    sections: list[str] = []
    summary: dict[str, Any] = {}
    for horizon in horizons:
        history = grouped.get(horizon, [])
        fan = _fan_chart(horizon, history)
        performance = _performance_chart(horizon, history, persistence, low_sample_threshold)
        section = f'<details class="chart-panel" open><summary>{_escape(horizon)} horizon</summary>{fan}{performance}</details>'
        if len("".join(sections)) + len(section) > MAX_CHART_PAYLOAD_BYTES:
            section = f'<details class="chart-panel"><summary>{_escape(horizon)} horizon</summary><p>Chart omitted to keep the static page within its payload budget.</p></details>'
        sections.append(section)
        summary[horizon] = {"matured_samples": len(history), "rendered_points": min(len(history), MAX_POINTS_PER_CHART)}
    table_rows = "".join(f"<tr><td>{_escape(horizon)}</td><td>{values['matured_samples']}</td><td>{values['rendered_points']}</td></tr>" for horizon, values in summary.items())
    fallback = '<div class="table-wrap"><table><caption>Chart data summary</caption><thead><tr><th>Horizon</th><th>Matured samples</th><th>Rendered points</th></tr></thead><tbody>' + table_rows + "</tbody></table></div>"
    return "".join(sections) + fallback, summary
