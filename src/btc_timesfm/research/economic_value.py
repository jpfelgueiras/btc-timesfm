#!/usr/bin/env python3
"""Economic-value evaluation for forecast research.

Evaluates whether statistically significant forecast improvements remain
meaningful after simple market-friction assumptions.  This is a research-only
sanity check: no execution, brokerage, or trading functionality is introduced.

Every evaluation is strictly out-of-sample: only matured durable-history rows
with ``actual_target_price_usd`` are consumed.  Results are reported per
horizon (2 h, 4 h, 8 h, 16 h) with turnover, gross effect, and
friction-adjusted effect.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from btc_timesfm.forecasting.statistical_significance import paired_bootstrap_comparison
from btc_timesfm.history.history_store import DEFAULT_DB_PATH, ENSEMBLE_MODEL, ForecastHistoryStore

DEFAULT_HORIZONS = (2, 4, 8, 16)
DEFAULT_LOW_SAMPLE_THRESHOLD = 20
DEFAULT_MIN_PAIRED_SAMPLES = 32
DEFAULT_BOOTSTRAP_ITERATIONS = 1000
DEFAULT_SEED = 42

PERSISTENCE_MODEL = "persistence"

# --- Default friction assumptions (transparent, configurable) ----------------
DEFAULT_ASSUMPTIONS: dict[str, Any] = {
    "round_trip_fee_pct": 0.1,
    "slippage_pct": 0.05,
    "decision_threshold_pct": 0.15,
    "min_meaningful_move_pct": 0.25,
    "max_daily_turnover": 6,
}


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _safe_int(value: Any) -> int | None:
    number = _safe_float(value)
    return int(number) if number is not None else None


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return sum(items) / len(items) if items else None


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None else None


def _sum(values: Iterable[float]) -> float:
    return sum(values)


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


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --- Signal extraction (out-of-sample only) --------------------------------


def _parse_signal(row: dict[str, Any], assumptions: dict[str, Any]) -> dict[str, Any] | None:
    """Parse a durable-history row into an economic-value signal record.

    Returns ``None`` for rows that cannot be evaluated (missing data or not yet
    matured).  Only ``actual_target_price_usd`` matured rows are consumed, so
    no future information is leaked.
    """
    if row.get("actual_target_price_usd") is None:
        return None
    predicted_change = _safe_float(row.get("predicted_change_pct"))
    actual_change = _safe_float(row.get("actual_change_pct"))
    horizon = _safe_float(row.get("horizon_hours"))
    origin_price = _safe_float(row.get("source_price_usd") or row.get("latest_close_usd"))
    target_price = _safe_float(row.get("actual_target_price_usd"))
    if (
        predicted_change is None
        or actual_change is None
        or horizon is None
        or origin_price is None
        or target_price is None
    ):
        return None

    decision_threshold = assumptions.get("decision_threshold_pct", 0.15)
    predicted_direction: str
    if predicted_change > decision_threshold:
        predicted_direction = "long"
    elif predicted_change < -decision_threshold:
        predicted_direction = "short"
    else:
        predicted_direction = "flat"

    actual_return_pct = ((target_price - origin_price) / origin_price) * 100.0
    if predicted_direction == "long":
        gross_effect_pct = actual_return_pct
    elif predicted_direction == "short":
        gross_effect_pct = -actual_return_pct
    else:
        gross_effect_pct = 0.0

    round_trip_fee = assumptions.get("round_trip_fee_pct", 0.1)
    slippage = assumptions.get("slippage_pct", 0.05)
    friction_pct = round_trip_fee + slippage
    friction_adjusted_effect_pct = (
        gross_effect_pct - friction_pct if predicted_direction != "flat" else 0.0
    )
    signal_active = predicted_direction != "flat"
    trade_count = 1 if signal_active else 0

    return {
        "origin_at": row.get("origin_at"),
        "target_at": row.get("target_at"),
        "model_name": str(row.get("model_name") or "unknown"),
        "horizon": f"{int(horizon)}h",
        "horizon_hours": int(horizon),
        "regime": str(row.get("regime") or "unknown"),
        "predicted_change_pct": predicted_change,
        "actual_return_pct": _round(actual_return_pct),
        "predicted_direction": predicted_direction,
        "gross_effect_pct": _round(gross_effect_pct),
        "friction_pct": _round(friction_pct),
        "friction_adjusted_effect_pct": _round(friction_adjusted_effect_pct),
        "signal_active": signal_active,
        "trade_count": trade_count,
    }


# --- Summary statistics -----------------------------------------------------


def _compute_turnover(signals: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute per-sample and aggregate turnover statistics."""
    total = len(signals)
    active = sum(1 for s in signals if s["signal_active"])
    return {
        "total_samples": total,
        "active_signal_samples": active,
        "turnover_rate": _round(active / total) if total else None,
    }


def _horizon_summary(
    signals: list[dict[str, Any]],
    *,
    assumptions: dict[str, Any],
    low_sample_threshold: int,
    min_paired_samples: int,
    bootstrap_iterations: int,
) -> dict[str, Any]:
    samples = len(signals)
    if samples == 0:
        return {
            "samples": 0,
            "turnover": _compute_turnover([]),
            "gross_effect_mean_pct": None,
            "friction_adjusted_effect_mean_pct": None,
            "total_friction_pct": None,
            "gross_positive_fraction": None,
            "significance": None,
            "practically_negligible": None,
            "reason": "no_samples",
            "unstable_or_low_sample": True,
        }

    gross_values = [s["gross_effect_pct"] for s in signals]
    adjusted_values = [s["friction_adjusted_effect_pct"] for s in signals]

    gross_mean = _mean(gross_values)
    adjusted_mean = _mean(adjusted_values)
    friction_per_trade = assumptions.get("round_trip_fee_pct", 0.1) + assumptions.get(
        "slippage_pct", 0.05
    )
    gross_positive = sum(1 for v in gross_values if v > 0)
    gross_positive_fraction = _round(gross_positive / samples) if samples else None

    # Bootstrap: compare friction-adjusted effects to zero (are we net-positive?)
    if len(adjusted_values) >= min_paired_samples:
        significance = paired_bootstrap_comparison(
            adjusted_values,
            [0.0] * len(adjusted_values),
            metric="friction_adjusted_effect_pct",
            lower_is_better=False,
            iterations=bootstrap_iterations,
            min_samples=min_paired_samples,
            seed=DEFAULT_SEED,
        )
    else:
        significance = {
            "conclusion": "inconclusive",
            "reason": "insufficient_samples",
            "probability_candidate_better": None,
            "improvement_ci": {"lower": None, "upper": None},
        }

    # Determine if statistically significant but practically negligible
    stat_sig = significance.get("conclusion") == "candidate_better"
    practically_meaningful_threshold = assumptions.get("min_meaningful_move_pct", 0.25)
    practically_negligible = (
        stat_sig
        and adjusted_mean is not None
        and abs(adjusted_mean) < practically_meaningful_threshold
    )

    unstable = samples < low_sample_threshold
    reason = "ok"
    if unstable:
        reason = f"low_sample_count:{samples}<{low_sample_threshold}"
    elif significance.get("conclusion") == "inconclusive":
        reason = significance.get("reason", "inconclusive")

    return {
        "samples": samples,
        "turnover": _compute_turnover(signals),
        "gross_effect_mean_pct": _round(gross_mean),
        "friction_adjusted_effect_mean_pct": _round(adjusted_mean),
        "total_friction_pct": _round(friction_per_trade),
        "gross_positive_fraction": gross_positive_fraction,
        "significance": {
            "conclusion": significance["conclusion"],
            "reason": significance.get("reason"),
            "probability_positive": significance.get("probability_candidate_better"),
            "ci_lower": significance.get("improvement_ci", {}).get("lower"),
            "ci_upper": significance.get("improvement_ci", {}).get("upper"),
        },
        "practically_negligible": practically_negligible,
        "reason": reason,
        "unstable_or_low_sample": unstable,
    }


# --- Main entry point -------------------------------------------------------


def build_report(
    rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    assumptions: dict[str, Any] | None = None,
    low_sample_threshold: int = DEFAULT_LOW_SAMPLE_THRESHOLD,
    min_paired_samples: int = DEFAULT_MIN_PAIRED_SAMPLES,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    model_name: str = ENSEMBLE_MODEL,
) -> dict[str, Any]:
    """Build a JSON-serializable economic-value evaluation report.

    Evaluation uses only matured out-of-sample rows.  No future data is leaked.
    Assumptions are explicit and configurable.
    """
    if low_sample_threshold < 1:
        raise ValueError("low_sample_threshold must be >= 1")
    if min_paired_samples < 1:
        raise ValueError("min_paired_samples must be >= 1")
    if bootstrap_iterations < 100:
        raise ValueError("bootstrap_iterations must be >= 100")

    effective_assumptions = {**DEFAULT_ASSUMPTIONS, **(assumptions or {})}
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    matured_rows = [row for row in rows if row.get("actual_target_price_usd") is not None]
    filtered_rows = [
        row for row in matured_rows if str(row.get("model_name") or "unknown") == model_name
    ]
    signals = [
        signal
        for row in filtered_rows
        for signal in [_parse_signal(row, effective_assumptions)]
        if signal is not None
    ]

    horizons = sorted(
        {f"{h}h" for h in DEFAULT_HORIZONS} | {s["horizon"] for s in signals},
        key=lambda label: int(label.rstrip("h")),
    )

    by_horizon: dict[str, Any] = {}
    for horizon in horizons:
        horizon_signals = [s for s in signals if s["horizon"] == horizon]
        by_horizon[horizon] = _horizon_summary(
            horizon_signals,
            assumptions=effective_assumptions,
            low_sample_threshold=low_sample_threshold,
            min_paired_samples=min_paired_samples,
            bootstrap_iterations=bootstrap_iterations,
        )

    all_turnover = _compute_turnover(signals)
    overall_adjusted = [s["friction_adjusted_effect_pct"] for s in signals]
    overall_gross = [s["gross_effect_pct"] for s in signals]

    # Overall significance
    if len(overall_adjusted) >= min_paired_samples:
        overall_sig = paired_bootstrap_comparison(
            overall_adjusted,
            [0.0] * len(overall_adjusted),
            metric="friction_adjusted_effect_pct",
            lower_is_better=False,
            iterations=bootstrap_iterations,
            min_samples=min_paired_samples,
            seed=DEFAULT_SEED,
        )
    else:
        overall_sig = {
            "conclusion": "inconclusive",
            "reason": "insufficient_samples",
            "probability_candidate_better": None,
            "improvement_ci": {"lower": None, "upper": None},
        }

    stat_sig = overall_sig.get("conclusion") == "candidate_better"
    overall_adj_mean = _mean(overall_adjusted)
    practically_meaningful_threshold = effective_assumptions.get("min_meaningful_move_pct", 0.25)
    overall_negligible = (
        stat_sig
        and overall_adj_mean is not None
        and abs(overall_adj_mean) < practically_meaningful_threshold
    )

    # Identify horizons that are statistically significant but negligible
    negligible_horizons = [h for h, s in by_horizon.items() if s.get("practically_negligible")]

    return {
        "generated_at": current_time.isoformat(),
        "model_name": model_name,
        "baseline_model": PERSISTENCE_MODEL,
        "assumptions": effective_assumptions,
        "horizons": horizons,
        "matured_rows": len(matured_rows),
        "evaluated_rows": len(filtered_rows),
        "evaluated_samples": len(signals),
        "overall_turnover": all_turnover,
        "overall_gross_effect_mean_pct": _round(_mean(overall_gross)),
        "overall_friction_adjusted_effect_mean_pct": _round(overall_adj_mean),
        "overall_significance": {
            "conclusion": overall_sig["conclusion"],
            "reason": overall_sig.get("reason"),
            "probability_positive": overall_sig.get("probability_candidate_better"),
            "ci_lower": overall_sig.get("improvement_ci", {}).get("lower"),
            "ci_upper": overall_sig.get("improvement_ci", {}).get("upper"),
        },
        "overall_practically_negligible": overall_negligible,
        "by_horizon": by_horizon,
        "negligible_horizons": negligible_horizons,
        "low_sample_threshold": low_sample_threshold,
        "leakage_guard": {
            "source": "durable_forecast_history",
            "uses_only_matured_outcomes": True,
            "strictly_out_of_sample": True,
            "no_execution_or_trading": True,
            "forecast_time_inputs": [
                "predicted_change_pct",
                "origin_at",
                "horizon_hours",
                "regime",
            ],
            "outcome_inputs": ["actual_change_pct", "actual_target_price_usd", "target_at"],
        },
    }


# --- Markdown rendering -----------------------------------------------------


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Economic-value evaluation",
        "",
        f"Generated: `{report['generated_at']}`",
        f"Model: **{report['model_name']}** vs baseline **{report['baseline_model']}**",
        f"Evaluated matured samples: **{report['evaluated_samples']}**",
        "",
        "## Assumptions",
        "",
        "| Parameter | Value |",
        "| --- | ---: |",
        f"| Round-trip fee | {_fmt(report['assumptions'].get('round_trip_fee_pct'))}% |",
        f"| Slippage | {_fmt(report['assumptions'].get('slippage_pct'))}% |",
        f"| Decision threshold | {_fmt(report['assumptions'].get('decision_threshold_pct'))}% |",
        f"| Min meaningful move | {_fmt(report['assumptions'].get('min_meaningful_move_pct'))}% |",
        "",
        "## Overall",
        "",
        "| Turnover rate | Gross effect | Friction-adjusted effect | Significance | Practically negligible |",
        "| ---: | ---: | ---: | --- | --- |",
        (
            f"| "
            f"{_fmt(report['overall_turnover'].get('turnover_rate'))} | "
            f"{_fmt(report['overall_gross_effect_mean_pct'])}% | "
            f"{_fmt(report['overall_friction_adjusted_effect_mean_pct'])}% | "
            f"{report['overall_significance']['conclusion']} | "
            f"{report['overall_practically_negligible']} |"
        ),
    ]

    if report.get("negligible_horizons"):
        lines.extend(
            [
                "",
                "**Horizons with statistically significant but practically negligible "
                f"improvements:** {', '.join(report['negligible_horizons'])}",
            ]
        )

    lines.extend(
        [
            "",
            "## By horizon",
            "",
            (
                "| Horizon | Samples | Turnover | Gross effect | Friction-adjusted | "
                "Significance | Negligible | Unstable |"
            ),
            "| --- | ---: | ---: | ---: | ---: | --- | --- | --- |",
        ]
    )
    for horizon in report["horizons"]:
        seg = report["by_horizon"][horizon]
        sig = seg.get("significance") or {}
        lines.append(
            f"| {horizon} | "
            f"{seg['samples']} | "
            f"{_fmt(seg['turnover'].get('turnover_rate'))} | "
            f"{_fmt(seg.get('gross_effect_mean_pct'))}% | "
            f"{_fmt(seg.get('friction_adjusted_effect_mean_pct'))}% | "
            f"{sig.get('conclusion', '—')} | "
            f"{seg.get('practically_negligible')} | "
            f"{seg.get('unstable_or_low_sample')} |"
        )

    lines.extend(
        [
            "",
            "---",
            "",
            "This is a **research-only** evaluation metric.  No execution, brokerage, or "
            "trading functionality is introduced.  All evaluations are strictly out-of-sample.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


# --- Report generation from database ----------------------------------------


def generate_report(
    db_path: Path,
    *,
    json_path: Path,
    markdown_path: Path,
    assumptions: dict[str, Any] | None = None,
    low_sample_threshold: int = DEFAULT_LOW_SAMPLE_THRESHOLD,
    min_paired_samples: int = DEFAULT_MIN_PAIRED_SAMPLES,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    model_name: str = ENSEMBLE_MODEL,
) -> dict[str, Any]:
    """Generate economic-value report from durable forecast history."""
    store = ForecastHistoryStore(db_path)
    verification = store.verify()
    verification_ok = (
        verification.get("integrity") == "ok"
        and int(verification.get("foreign_key_violations", 1)) == 0
        and verification.get("schema_version") == verification.get("supported_schema_version")
    )
    if not verification_ok:
        raise RuntimeError(f"forecast history failed verification: {verification}")
    report = build_report(
        store.export_rows(),
        assumptions=assumptions,
        low_sample_threshold=low_sample_threshold,
        min_paired_samples=min_paired_samples,
        bootstrap_iterations=bootstrap_iterations,
        model_name=model_name,
    )
    report["database_verification"] = {**verification, "ok": True}
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate economic-value evaluation from durable forecast history"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--json", type=Path, default=Path("economic_value_report.json"))
    parser.add_argument("--markdown", type=Path, default=Path("economic_value_report.md"))
    parser.add_argument("--model-name", default=ENSEMBLE_MODEL)
    parser.add_argument(
        "--round-trip-fee-pct", type=float, default=DEFAULT_ASSUMPTIONS["round_trip_fee_pct"]
    )
    parser.add_argument("--slippage-pct", type=float, default=DEFAULT_ASSUMPTIONS["slippage_pct"])
    parser.add_argument(
        "--decision-threshold-pct",
        type=float,
        default=DEFAULT_ASSUMPTIONS["decision_threshold_pct"],
    )
    parser.add_argument(
        "--min-meaningful-move-pct",
        type=float,
        default=DEFAULT_ASSUMPTIONS["min_meaningful_move_pct"],
    )
    parser.add_argument("--low-sample-threshold", type=int, default=DEFAULT_LOW_SAMPLE_THRESHOLD)
    parser.add_argument("--min-paired-samples", type=int, default=DEFAULT_MIN_PAIRED_SAMPLES)
    parser.add_argument("--bootstrap-iterations", type=int, default=DEFAULT_BOOTSTRAP_ITERATIONS)
    args = parser.parse_args()

    assumptions = {
        "round_trip_fee_pct": args.round_trip_fee_pct,
        "slippage_pct": args.slippage_pct,
        "decision_threshold_pct": args.decision_threshold_pct,
        "min_meaningful_move_pct": args.min_meaningful_move_pct,
    }
    report = generate_report(
        args.db,
        json_path=args.json,
        markdown_path=args.markdown,
        assumptions=assumptions,
        low_sample_threshold=args.low_sample_threshold,
        min_paired_samples=args.min_paired_samples,
        bootstrap_iterations=args.bootstrap_iterations,
        model_name=args.model_name,
    )
    print(
        json.dumps(
            {
                "generated_at": report["generated_at"],
                "evaluated_samples": report["evaluated_samples"],
                "overall_practically_negligible": report["overall_practically_negligible"],
                "negligible_horizons": report["negligible_horizons"],
                "json": str(args.json),
                "markdown": str(args.markdown),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
