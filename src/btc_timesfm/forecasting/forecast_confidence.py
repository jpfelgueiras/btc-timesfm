"""Statistically grounded forecast-confidence diagnostics.

The score is a quality/evidence band, not a probability that BTC will move in the
predicted direction.  It only uses already-matured out-of-sample history plus
the current forecast interval and current drift state.
"""

from __future__ import annotations

import math
from typing import Any

HORIZONS = ("2h", "4h", "8h", "16h")
CONFIDENCE_VERSION = 1
MIN_EVIDENCE_SAMPLES = 20
STRONG_EVIDENCE_SAMPLES = 40
HIGH_SCORE = 70.0
MODERATE_SCORE = 45.0


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _sample_factor(samples: int) -> float:
    if samples <= 0:
        return 0.0
    return _clamp(math.sqrt(samples / STRONG_EVIDENCE_SAMPLES))


def _edge_factor(edge_pct: float) -> float:
    """Map relative MAE edge versus persistence to a bounded quality factor."""
    if edge_pct <= 0.0:
        return _clamp(0.15 + edge_pct / 20.0)
    if edge_pct < 5.0:
        return 0.45 + 0.09 * edge_pct
    return _clamp(0.90 + 0.01 * (edge_pct - 5.0))


def _coverage_factor(empirical: float, target: float) -> float:
    """Full credit at target coverage, fading to zero at a 20pp miss."""
    return _clamp(1.0 - abs(empirical - target) / 0.20)


def _width_factor(current_width_pct: float, typical_width_pct: float | None) -> float:
    """Penalize intervals that are unusually wide relative to calibrated history."""
    if typical_width_pct is None or typical_width_pct <= 0:
        return 0.75
    ratio = current_width_pct / typical_width_pct
    if ratio <= 1.0:
        return 1.0
    if ratio <= 2.0:
        return 1.0 - 0.60 * (ratio - 1.0)
    return _clamp(0.40 - 0.15 * (ratio - 2.0), 0.20, 0.40)


def _drift_factor(drift_report: dict[str, Any]) -> tuple[str, float]:
    severity = str(drift_report.get("severity") or "none").lower()
    adaptive = _finite_float(drift_report.get("adaptive_confidence"))
    adaptive = _clamp(adaptive) if adaptive is not None else 1.0
    if severity == "severe":
        return severity, 0.0
    if severity == "warning":
        return severity, min(0.65, adaptive)
    return severity, adaptive


def _confidence_label(
    score: float,
    *,
    edge_pct: float,
    coverage_gap: float,
    samples: int,
    drift_severity: str,
) -> str:
    # "high" deliberately has stronger hard requirements than the numeric score.
    if (
        score >= HIGH_SCORE
        and edge_pct > 0.0
        and coverage_gap <= 0.05
        and samples >= STRONG_EVIDENCE_SAMPLES
        and drift_severity == "none"
    ):
        return "high"
    if score >= MODERATE_SCORE:
        return "moderate"
    return "low"


def _persistence_edge(
    performance: dict[str, Any],
) -> tuple[float | None, float | None, float | None]:
    ensemble_mae = _finite_float(performance.get("mae_pct"))
    models = performance.get("models")
    persistence_mae = None
    if isinstance(models, dict):
        persistence = models.get("persistence")
        if isinstance(persistence, dict):
            persistence_mae = _finite_float(persistence.get("mae_pct"))
    if ensemble_mae is None or persistence_mae is None or persistence_mae <= 0:
        return None, ensemble_mae, persistence_mae
    edge_pct = (persistence_mae - ensemble_mae) / persistence_mae * 100.0
    return edge_pct, ensemble_mae, persistence_mae


def _interval_width_pct(prediction: dict[str, Any]) -> float | None:
    point = _finite_float(prediction.get("price_usd"))
    q10 = _finite_float(prediction.get("q10_usd"))
    q90 = _finite_float(prediction.get("q90_usd"))
    if point is None or q10 is None or q90 is None or point <= 0 or q90 <= q10:
        return None
    return (q90 - q10) / point * 100.0


def horizon_confidence(
    horizon: str,
    prediction: dict[str, Any] | None,
    performance: dict[str, Any] | None,
    calibration: dict[str, Any] | None,
    drift_report: dict[str, Any],
) -> dict[str, Any]:
    """Build confidence diagnostics for one horizon from matured OOS evidence."""
    prediction = prediction if isinstance(prediction, dict) else {}
    performance = performance if isinstance(performance, dict) else {}
    calibration = calibration if isinstance(calibration, dict) else {}

    performance_samples = int(performance.get("samples") or 0)
    calibration_samples = int(calibration.get("samples") or 0)
    evidence_samples = min(performance_samples, calibration_samples)

    edge_pct, ensemble_mae, persistence_mae = _persistence_edge(performance)
    empirical_coverage = _finite_float(calibration.get("empirical_coverage_after"))
    target_coverage = _finite_float(calibration.get("target_coverage"))
    current_width = _interval_width_pct(prediction)
    typical_width = _finite_float(calibration.get("average_interval_width_pct_after"))
    direction_accuracy = _finite_float(performance.get("direction_accuracy"))
    drift_severity, drift_factor = _drift_factor(drift_report)

    diagnostics: dict[str, Any] = {
        "status": "available",
        "label": None,
        "score": None,
        "performance_samples": performance_samples,
        "calibration_samples": calibration_samples,
        "evidence_samples": evidence_samples,
        "ensemble_mae_pct": ensemble_mae,
        "persistence_mae_pct": persistence_mae,
        "edge_vs_persistence_pct": round(edge_pct, 3) if edge_pct is not None else None,
        "direction_accuracy": direction_accuracy,
        "target_coverage": target_coverage,
        "empirical_calibrated_coverage": empirical_coverage,
        "current_interval_width_pct": round(current_width, 3)
        if current_width is not None
        else None,
        "typical_calibrated_interval_width_pct": (
            round(typical_width, 3) if typical_width is not None else None
        ),
        "calibration_mode": calibration.get("mode"),
        "calibration_source": calibration.get("source"),
        "drift_severity": drift_severity,
        "drift_factor": round(drift_factor, 3),
        "factors": None,
        "reasons": [],
    }

    if drift_severity == "severe":
        diagnostics["status"] = "suppressed_drift"
        diagnostics["reasons"] = ["severe production drift suppresses confidence claims"]
        return diagnostics

    missing: list[str] = []
    if evidence_samples < MIN_EVIDENCE_SAMPLES:
        missing.append(
            f"requires at least {MIN_EVIDENCE_SAMPLES} matured performance and calibration samples"
        )
    if edge_pct is None:
        missing.append("persistence baseline MAE is unavailable")
    if empirical_coverage is None or target_coverage is None:
        missing.append("calibrated historical coverage is unavailable")
    if current_width is None:
        missing.append("current calibrated interval is unavailable")

    if missing:
        diagnostics["status"] = "insufficient_evidence"
        diagnostics["reasons"] = missing
        return diagnostics

    assert edge_pct is not None
    assert empirical_coverage is not None
    assert target_coverage is not None
    assert current_width is not None

    sample_factor = _sample_factor(evidence_samples)
    edge_factor = _edge_factor(edge_pct)
    coverage_factor = _coverage_factor(empirical_coverage, target_coverage)
    width_factor = _width_factor(current_width, typical_width)
    raw_score = 100.0 * (
        0.35 * edge_factor + 0.30 * coverage_factor + 0.20 * sample_factor + 0.15 * width_factor
    )
    score = raw_score * drift_factor
    coverage_gap = abs(empirical_coverage - target_coverage)

    # A forecast that has not beaten persistence, or whose interval calibration is
    # badly off target, cannot be advertised above the low band.
    if edge_pct <= 0.0 or coverage_gap > 0.15:
        score = min(score, MODERATE_SCORE - 1.0)
    # Warning drift may still leave useful evidence, but it cannot support HIGH.
    if drift_severity == "warning":
        score = min(score, HIGH_SCORE - 1.0)

    score = round(_clamp(score / 100.0) * 100.0, 1)
    label = _confidence_label(
        score,
        edge_pct=edge_pct,
        coverage_gap=coverage_gap,
        samples=evidence_samples,
        drift_severity=drift_severity,
    )

    diagnostics["score"] = score
    diagnostics["label"] = label
    diagnostics["factors"] = {
        "edge_vs_persistence": round(edge_factor, 3),
        "calibration_coverage": round(coverage_factor, 3),
        "sample_depth": round(sample_factor, 3),
        "interval_informativeness": round(width_factor, 3),
    }
    reasons = [
        f"{edge_pct:+.1f}% relative MAE edge versus persistence",
        f"calibrated coverage {empirical_coverage:.0%} vs {target_coverage:.0%} target",
        f"{evidence_samples} matured evidence samples",
    ]
    if drift_severity == "warning":
        reasons.append("warning drift reduced the score")
    diagnostics["reasons"] = reasons
    return diagnostics


def build_forecast_confidence(
    predictions: dict[str, Any],
    performance_summary: dict[str, Any],
    interval_calibration: dict[str, Any],
    drift_report: dict[str, Any],
) -> dict[str, Any]:
    """Return per-horizon and conservative overall confidence diagnostics."""
    horizon_results: dict[str, Any] = {}
    for horizon in HORIZONS:
        horizon_results[horizon] = horizon_confidence(
            horizon,
            predictions.get(horizon) if isinstance(predictions, dict) else None,
            performance_summary.get(horizon) if isinstance(performance_summary, dict) else None,
            interval_calibration.get(horizon) if isinstance(interval_calibration, dict) else None,
            drift_report if isinstance(drift_report, dict) else {},
        )

    available = [item for item in horizon_results.values() if item["status"] == "available"]
    result: dict[str, Any] = {
        "version": CONFIDENCE_VERSION,
        "meaning": "evidence-quality band, not probability of forecast success",
        "status": "available",
        "label": None,
        "score": None,
        "minimum_evidence_samples": None,
        "minimum_edge_vs_persistence_pct": None,
        "horizons": horizon_results,
        "configuration": {
            "minimum_evidence_samples": MIN_EVIDENCE_SAMPLES,
            "strong_evidence_samples": STRONG_EVIDENCE_SAMPLES,
            "moderate_score_threshold": MODERATE_SCORE,
            "high_score_threshold": HIGH_SCORE,
            "weights": {
                "edge_vs_persistence": 0.35,
                "calibration_coverage": 0.30,
                "sample_depth": 0.20,
                "interval_informativeness": 0.15,
            },
        },
    }

    statuses = {item["status"] for item in horizon_results.values()}
    if "suppressed_drift" in statuses:
        result["status"] = "suppressed_drift"
        return result
    if len(available) != len(HORIZONS):
        result["status"] = "insufficient_evidence"
        return result

    scores = [float(item["score"]) for item in available]
    samples = [int(item["evidence_samples"]) for item in available]
    edges = [float(item["edge_vs_persistence_pct"]) for item in available]

    # The public post covers all four horizons, so the overall band is deliberately
    # bounded by the weakest horizon rather than averaging away a weak forecast.
    overall_score = min(scores)
    result["score"] = round(overall_score, 1)
    result["label"] = min(
        (str(item["label"]) for item in available),
        key={"low": 0, "moderate": 1, "high": 2}.__getitem__,
    )
    result["minimum_evidence_samples"] = min(samples)
    result["minimum_edge_vs_persistence_pct"] = round(min(edges), 3)
    return result
