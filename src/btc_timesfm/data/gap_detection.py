"""Harden upstream market-data gap handling into an auditable, policy-driven state.

Kraken/Bitstamp hourly candles can arrive missing, delayed, or already repaired.
This module makes those conditions first class:

* **Reconciliation** — the redundant sources are compared per overlapping candle and
  divergence, missing timestamps (gaps) and silent-fill candidates are reported.
* **Gap policy** — machine-readable rules decide, for every gap, whether to
  fill from the secondary provider (``fill``), fill with a marked row and degrade
  confidence (``degrade``), or withhold the forecast (``withhold``).
* **Backfill correctness** — backfills write a provenance record for every filled
  candle and re-running the backfill on its own output produces identical history.
* **Forecast state** — ``forecast_state()`` produces a drop-in JSON section that can
  flow into ``forecast.json`` and the site's forecast fields.

The reconciliation report is published per run as ``gap_reconciliation.json`` and
exposes every gap, divergence, silent-fill candidate and provenance record so a
corrected value is never indistinguishable from genuinely observed data.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence

import numpy as np


GAP_REPORT_PATH = Path("gap_reconciliation.json")
GAP_SCHEMA_VERSION = 1

# Machine-readable classifications for a single detected gap.
CLASSIFICATION_FILLED_BY_SECONDARY = "filled_by_secondary"
CLASSIFICATION_MARKED_IN_HISTORY = "marked_in_history"
CLASSIFICATION_REQUIRES_WITHHOLDING = "requires_withholding"

# Machine-readable policy actions emitted by the gap rules.
ACTION_FILL = "fill"
ACTION_DEGRADE = "degrade"
ACTION_WITHHOLD = "withhold"


class MarketDataLike(Protocol):
    """Minimal OHLCV surface shared by live providers and repaired series."""

    timestamps: Sequence[int]
    opens: Any
    highs: Any
    lows: Any
    closes: Any
    volumes: Any


@dataclass(frozen=True)
class GapConfig:
    interval_seconds: int = 3600
    allowed_missing_candles: int = 1
    fill_max_gap_candles: int = 3
    comparison_candles: int = 24
    min_overlap_candles: int = 6
    divergence_threshold_pct: float = 0.75
    report_gap_limit: int = 200

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if self.allowed_missing_candles < 1:
            raise ValueError("allowed_missing_candles must be >= 1")
        if self.fill_max_gap_candles < self.allowed_missing_candles:
            raise ValueError("fill_max_gap_candles must be >= allowed_missing_candles")
        if self.comparison_candles < 1:
            raise ValueError("comparison_candles must be >= 1")
        if self.min_overlap_candles < 1:
            raise ValueError("min_overlap_candles must be >= 1")
        if self.divergence_threshold_pct <= 0:
            raise ValueError("divergence_threshold_pct must be positive")
        if self.report_gap_limit < 1:
            raise ValueError("report_gap_limit must be >= 1")

    @classmethod
    def from_env(cls) -> "GapConfig":
        return cls(
            interval_seconds=_env_int("BTC_DATA_INTERVAL_SECONDS", cls.interval_seconds),
            allowed_missing_candles=_env_int(
                "BTC_GAP_ALLOWED_MISSING_CANDLES", cls.allowed_missing_candles
            ),
            fill_max_gap_candles=_env_int("BTC_GAP_FILL_MAX_CANDLES", cls.fill_max_gap_candles),
            comparison_candles=_env_int("BTC_GAP_COMPARE_CANDLES", cls.comparison_candles),
            min_overlap_candles=_env_int("BTC_GAP_MIN_OVERLAP", cls.min_overlap_candles),
            divergence_threshold_pct=_env_float(
                "BTC_GAP_DIVERGENCE_THRESHOLD_PCT", cls.divergence_threshold_pct
            ),
            report_gap_limit=_env_int("BTC_GAP_REPORT_LIMIT", cls.report_gap_limit),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GapPolicyRule:
    rule_id: str
    condition: str
    classification: str
    action: str
    reason: str


GAP_POLICY_RULES: tuple[GapPolicyRule, ...] = (
    GapPolicyRule(
        rule_id="fill-if-covered",
        condition=(
            "gap size is within fill_max_gap_candles and every missing candle is "
            "present in the secondary provider with no overlap disagreement"
        ),
        classification=CLASSIFICATION_FILLED_BY_SECONDARY,
        action=ACTION_FILL,
        reason="secondary covers the gap within tolerance; candles are backfilled with a provenance record",
    ),
    GapPolicyRule(
        rule_id="fill-with-mark-degrade-if-mismatch",
        condition=(
            "gap is covered by the secondary but the recent overlap diverges beyond "
            "tolerance, or the gap is small and uncovered"
        ),
        classification=CLASSIFICATION_MARKED_IN_HISTORY,
        action=ACTION_DEGRADE,
        reason="corrected candles are marked in history and forecast confidence is degraded",
    ),
    GapPolicyRule(
        rule_id="withhold-if-uncovered",
        condition=(
            "gap is not covered by the secondary and exceeds allowed_missing_candles, "
            "or the gap is larger than fill_max_gap_candles"
        ),
        classification=CLASSIFICATION_REQUIRES_WITHHOLDING,
        action=ACTION_WITHHOLD,
        reason="gap cannot be repaired from a trusted source, so the forecast is withheld",
    ),
)


def gap_policy_rules() -> tuple[dict[str, Any], ...]:
    return tuple(asdict(rule) for rule in GAP_POLICY_RULES)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return int(default)
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return float(default)
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalized_now(now: datetime | None) -> datetime:
    checked = now or _utc_now()
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    return checked.astimezone(timezone.utc)


@dataclass(frozen=True)
class MissingCandleRun:
    first_timestamp: int
    timestamps: tuple[int, ...]
    missing_candles: int
    gap_seconds: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "first_timestamp": self.first_timestamp,
            "timestamps": [int(value) for value in self.timestamps],
            "missing_candles": self.missing_candles,
            "gap_seconds": self.gap_seconds,
        }


def detect_missing_candles(
    timestamps: Sequence[int],
    *,
    interval_seconds: int = 3600,
) -> list[MissingCandleRun]:
    """Return runs of expected-but-missing candle timestamps in a series.

    A candle is missing when two consecutive timestamps differ by more than one
    cadence interval. Runs are grouped so ``(t, t+interval)`` form a single gap.
    """
    sorted_ts = sorted({int(value) for value in timestamps})
    missing: list[int] = []
    for prior, current in zip(sorted_ts, sorted_ts[1:]):
        diff = current - prior
        if diff <= 0:
            continue
        steps = diff // interval_seconds
        if steps <= 1:
            continue
        missing.extend(prior + step * interval_seconds for step in range(1, steps))

    missing.sort()
    runs: list[MissingCandleRun] = []
    run: list[int] = []
    previous: int | None = None
    for value in missing:
        if previous is not None and value == previous + interval_seconds:
            run.append(value)
        else:
            if run:
                runs.append(
                    MissingCandleRun(
                        first_timestamp=run[0],
                        timestamps=tuple(run),
                        missing_candles=len(run),
                        gap_seconds=len(run) * interval_seconds,
                    )
                )
            run = [value]
        previous = value
    if run:
        runs.append(
            MissingCandleRun(
                first_timestamp=run[0],
                timestamps=tuple(run),
                missing_candles=len(run),
                gap_seconds=len(run) * interval_seconds,
            )
        )
    return runs


def compare_redundant_overlap(
    primary: MarketDataLike,
    secondary: MarketDataLike,
    *,
    config: GapConfig,
) -> dict[str, Any]:
    """Compare overlapping closes from the redundant providers per candle."""
    primary_closes = dict(zip(primary.timestamps, map(float, primary.closes), strict=True))
    secondary_closes = dict(zip(secondary.timestamps, map(float, secondary.closes), strict=True))
    common = sorted(set(primary_closes) & set(secondary_closes))
    if config.comparison_candles > 0:
        common = common[-config.comparison_candles :]
    if len(common) < config.min_overlap_candles:
        return {
            "status": "insufficient_overlap",
            "overlap_candles": len(common),
            "required_overlap_candles": config.min_overlap_candles,
            "max_close_difference_pct": None,
            "mean_close_difference_pct": None,
            "tolerance_pct": config.divergence_threshold_pct,
            "divergent_candles": [],
        }

    differences: list[dict[str, Any]] = []
    for timestamp in common:
        left = primary_closes[timestamp]
        right = secondary_closes[timestamp]
        midpoint = (left + right) / 2.0
        difference = abs(left - right) / midpoint * 100.0 if midpoint > 0 else float("inf")
        differences.append(
            {
                "timestamp": int(timestamp),
                "primary_close": round(float(left), 4),
                "secondary_close": round(float(right), 4),
                "difference_pct": round(float(difference), 6),
                "within_tolerance": float(difference) <= config.divergence_threshold_pct,
            }
        )
    values = [float(item["difference_pct"]) for item in differences]
    maximum = max(values)
    divergent = [item for item in differences if not item["within_tolerance"]]
    return {
        "status": "ok" if maximum <= config.divergence_threshold_pct else "disagreement",
        "overlap_candles": len(common),
        "first_overlap_at": common[0],
        "latest_overlap_at": common[-1],
        "max_close_difference_pct": round(maximum, 6),
        "mean_close_difference_pct": round(float(np.mean(values)), 6),
        "tolerance_pct": config.divergence_threshold_pct,
        "divergent_candles": [int(item["timestamp"]) for item in divergent],
    }


@dataclass(frozen=True)
class GapDecision:
    first_timestamp: int
    gap_seconds: int
    missing_candles: int
    covered_by_secondary: int
    classification: str
    action: str
    rule_id: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def decide_gap(
    *,
    first_timestamp: int,
    missing_candles: int,
    covered_by_secondary: int,
    overlap_status: str,
    config: GapConfig,
) -> GapDecision:
    """Evaluate one gap against the machine-readable policy rules.

    * ``fill`` — the secondary fully covers the gap within ``fill_max_gap_candles``
      and the recent overlap agrees.
    * ``degrade`` — the gap would be covered but the overlap disagrees, or the gap is
      small enough to tolerate uncovered (marked in history, confidence degraded).
    * ``withhold`` — the gap is uncovered beyond ``allowed_missing_candles`` or too
      large to backfill safely.
    """
    seconds = missing_candles * config.interval_seconds
    fully_covered = missing_candles > 0 and covered_by_secondary == missing_candles
    divergence_ok = overlap_status == "ok"

    if missing_candles > config.fill_max_gap_candles:
        return GapDecision(
            first_timestamp=first_timestamp,
            gap_seconds=seconds,
            missing_candles=missing_candles,
            covered_by_secondary=covered_by_secondary,
            classification=CLASSIFICATION_REQUIRES_WITHHOLDING,
            action=ACTION_WITHHOLD,
            rule_id="withhold-if-uncovered",
            reason=f"gap of {missing_candles} candles is larger than the safe backfill window "
            f"({config.fill_max_gap_candles})",
        )
    if fully_covered and divergence_ok:
        return GapDecision(
            first_timestamp=first_timestamp,
            gap_seconds=seconds,
            missing_candles=missing_candles,
            covered_by_secondary=covered_by_secondary,
            classification=CLASSIFICATION_FILLED_BY_SECONDARY,
            action=ACTION_FILL,
            rule_id="fill-if-covered",
            reason=f"secondary covers all {missing_candles} missing candles within divergence tolerance",
        )
    if fully_covered:
        return GapDecision(
            first_timestamp=first_timestamp,
            gap_seconds=seconds,
            missing_candles=missing_candles,
            covered_by_secondary=covered_by_secondary,
            classification=CLASSIFICATION_MARKED_IN_HISTORY,
            action=ACTION_DEGRADE,
            rule_id="fill-with-mark-degrade-if-mismatch",
            reason=(
                f"secondary covers the gap but overlapping candles diverge beyond "
                f"{config.divergence_threshold_pct:.2f}%"
            ),
        )
    if missing_candles <= config.allowed_missing_candles:
        return GapDecision(
            first_timestamp=first_timestamp,
            gap_seconds=seconds,
            missing_candles=missing_candles,
            covered_by_secondary=covered_by_secondary,
            classification=CLASSIFICATION_MARKED_IN_HISTORY,
            action=ACTION_DEGRADE,
            rule_id="fill-with-mark-degrade-if-mismatch",
            reason=f"minor uncovered gap of {missing_candles} candle is marked in history with degraded confidence",
        )
    return GapDecision(
        first_timestamp=first_timestamp,
        gap_seconds=seconds,
        missing_candles=missing_candles,
        covered_by_secondary=covered_by_secondary,
        classification=CLASSIFICATION_REQUIRES_WITHHOLDING,
        action=ACTION_WITHHOLD,
        rule_id="withhold-if-uncovered",
        reason=f"secondary does not cover the {missing_candles}-candle gap; the forecast is withheld",
    )


def detect_silent_fills(
    primary: MarketDataLike,
    secondary: MarketDataLike,
    *,
    config: GapConfig,
) -> list[dict[str, Any]]:
    """Flag candles a naive merge could adopt from the secondary with no trail.

    A kicker on the regular cadence that exists only in the secondary is the exact
    row a broken pipeline would silently inject. Every such row must carry a
    provenance record; until it does, it is reported as a silent-fill candidate.
    """
    primary_set = {int(value) for value in primary.timestamps}
    secondary_set = {int(value) for value in secondary.timestamps}
    candidates: list[dict[str, Any]] = []
    for run in detect_missing_candles(primary.timestamps, interval_seconds=config.interval_seconds):
        for timestamp in run.timestamps:
            if timestamp not in secondary_set:
                continue
            candidates.append(
                {
                    "timestamp": int(timestamp),
                    "source": "secondary",
                    "kind": "gap_fill_candidate",
                    "classification": CLASSIFICATION_FILLED_BY_SECONDARY,
                    "remediation": (
                        "backfill must write a provenance record or the value remains "
                        "a silent fill indistinguishable from observed data"
                    ),
                }
            )
    anchor = min(primary_set, default=0)
    for timestamp in sorted(secondary_set):
        if timestamp in primary_set:
            continue
        if (timestamp - anchor) % config.interval_seconds != 0:
            candidates.append(
                {
                    "timestamp": int(timestamp),
                    "source": "secondary",
                    "kind": "off_lattice_candle",
                    "classification": CLASSIFICATION_MARKED_IN_HISTORY,
                    "remediation": (
                        "candle is off the regular cadence; it must be marked before use"
                    ),
                }
            )
    candidates.sort(key=lambda item: int(item["timestamp"]))
    return candidates


@dataclass
class BackfillResult:
    timestamps: Sequence[int]
    opens: np.ndarray
    highs: np.ndarray
    lows: np.ndarray
    closes: np.ndarray
    volumes: np.ndarray
    provenance: list[dict[str, Any]]


def backfill_from_secondary(
    primary: MarketDataLike,
    secondary: MarketDataLike,
    *,
    config: GapConfig,
    existing_provenance: Iterable[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> BackfillResult:
    """Repair missing primary candles from the secondary with a provenance trail.

    Provenance is keyed by candle timestamp and first write wins, so feeding a
    prior run's ``existing_provenance`` back in is idempotent.
    """
    checked = _normalized_now(now)
    secondary_index = {
        int(timestamp): index for index, timestamp in enumerate(secondary.timestamps)
    }

    fills: list[tuple[int, int]] = []
    for run in detect_missing_candles(primary.timestamps, interval_seconds=config.interval_seconds):
        if run.missing_candles > config.fill_max_gap_candles:
            continue
        for timestamp in run.timestamps:
            index = secondary_index.get(timestamp)
            if index is not None:
                fills.append((timestamp, index))
    fills.sort(key=lambda item: item[0])

    sec_opens = np.asarray(secondary.opens, dtype=np.float64)
    sec_highs = np.asarray(secondary.highs, dtype=np.float64)
    sec_lows = np.asarray(secondary.lows, dtype=np.float64)
    sec_closes = np.asarray(secondary.closes, dtype=np.float64)
    sec_volumes = np.asarray(secondary.volumes, dtype=np.float64)

    provenance_map: dict[int, dict[str, Any]] = {}
    for existing in existing_provenance or ():
        if not isinstance(existing, dict):
            continue
        try:
            timestamp = int(existing["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        if timestamp not in provenance_map:
            provenance_map[timestamp] = existing

    filled_at = checked.isoformat()
    for timestamp, _ in fills:
        if timestamp in provenance_map:
            continue
        provenance_map[timestamp] = {
            "timestamp": timestamp,
            "source": "secondary",
            "action": ACTION_FILL,
            "classification": CLASSIFICATION_FILLED_BY_SECONDARY,
            "rule_id": "fill-if-covered",
            "filled_at": filled_at,
            "schema_version": GAP_SCHEMA_VERSION,
        }

    filled_timestamps = {int(timestamp) for timestamp, _ in fills}
    primary_positions = {
        int(timestamp): index for index, timestamp in enumerate(primary.timestamps)
    }
    merged_timestamps = sorted(set(primary_positions) | filled_timestamps)

    primary_open = list(np.asarray(primary.opens, dtype=np.float64))
    primary_high = list(np.asarray(primary.highs, dtype=np.float64))
    primary_low = list(np.asarray(primary.lows, dtype=np.float64))
    primary_close = list(np.asarray(primary.closes, dtype=np.float64))
    primary_volume = list(np.asarray(primary.volumes, dtype=np.float64))

    fill_indices: dict[int, int] = dict(fills)
    out_ts: list[int] = []
    out_open: list[float] = []
    out_high: list[float] = []
    out_low: list[float] = []
    out_close: list[float] = []
    out_volume: list[float] = []
    for timestamp in merged_timestamps:
        position = primary_positions.get(timestamp)
        if position is not None:
            out_ts.append(timestamp)
            out_open.append(primary_open[position])
            out_high.append(primary_high[position])
            out_low.append(primary_low[position])
            out_close.append(primary_close[position])
            out_volume.append(primary_volume[position])
            continue
        index = fill_indices.get(timestamp)
        if index is None:
            continue
        out_ts.append(timestamp)
        out_open.append(float(sec_opens[index]))
        out_high.append(float(sec_highs[index]))
        out_low.append(float(sec_lows[index]))
        out_close.append(float(sec_closes[index]))
        out_volume.append(float(sec_volumes[index]))

    out_ts_set = set(out_ts)
    provenance_sorted = [
        provenance_map[timestamp] for timestamp in sorted(provenance_map) if timestamp in out_ts_set
    ]
    return BackfillResult(
        timestamps=out_ts,
        opens=np.asarray(out_open, dtype=np.float32),
        highs=np.asarray(out_high, dtype=np.float32),
        lows=np.asarray(out_low, dtype=np.float32),
        closes=np.asarray(out_close, dtype=np.float32),
        volumes=np.asarray(out_volume, dtype=np.float32),
        provenance=provenance_sorted,
    )


def verify_backfill_idempotency(
    primary: MarketDataLike,
    secondary: MarketDataLike,
    *,
    config: GapConfig,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Re-running the backfill on its own output must produce identical history."""
    checked = _normalized_now(now)
    first = backfill_from_secondary(primary, secondary, config=config, now=checked)
    rerun = backfill_from_secondary(
        first,
        secondary,
        config=config,
        existing_provenance=first.provenance,
        now=checked,
    )
    identical_timestamps = rerun.timestamps == first.timestamps
    identical_ohlcv = all(
        np.array_equal(getattr(rerun, attribute), getattr(first, attribute))
        for attribute in ("opens", "highs", "lows", "closes", "volumes")
    )
    identical_provenance = first.provenance == rerun.provenance
    remaining_gaps = detect_missing_candles(
        rerun.timestamps, interval_seconds=config.interval_seconds
    )
    return {
        "idempotent": bool(identical_timestamps and identical_ohlcv and identical_provenance),
        "identical_timestamps": bool(identical_timestamps),
        "identical_ohlcv": bool(identical_ohlcv),
        "identical_provenance": bool(identical_provenance),
        "remaining_gaps": len(remaining_gaps),
        "filled_candles": len(first.provenance),
        "provenance_stable": bool(identical_provenance),
    }


def apply_gap_policy(decisions: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-gap decisions into a run-level machine-readable policy output."""
    decision_list = [item for item in decisions if isinstance(item, dict)]
    actions = [str(item.get("action")) for item in decision_list if item.get("action")]
    if ACTION_WITHHOLD in actions:
        applied = ACTION_WITHHOLD
    elif ACTION_DEGRADE in actions:
        applied = ACTION_DEGRADE
    elif ACTION_FILL in actions:
        applied = ACTION_FILL
    else:
        applied = "none"
    reasons = {
        ACTION_WITHHOLD: "at least one gap could not be repaired from a trusted source",
        ACTION_DEGRADE: "gaps were repaired with marked rows; confidence is degraded",
        ACTION_FILL: "gaps were repaired from the secondary with provenance records",
        "none": "no gaps detected; no action required",
    }
    return {
        "policy_applied": applied,
        "withhold_forecast": applied == ACTION_WITHHOLD,
        "degrade_confidence": applied == ACTION_DEGRADE,
        "filled_any": applied == ACTION_FILL,
        "decided_by": reasons[applied],
        "rules": gap_policy_rules(),
    }


def forecast_state(
    decisions: Iterable[dict[str, Any]],
    *,
    counts: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Drop-in gap state for ``forecast.json`` and the site's forecast fields."""
    decision_list = [item for item in decisions if isinstance(item, dict)]
    actions = [str(item.get("action")) for item in decision_list if item.get("action")]
    applied = policy or apply_gap_policy(decision_list)
    counted = counts or {}
    return {
        "version": GAP_SCHEMA_VERSION,
        "action": str(applied.get("policy_applied") or "none"),
        "withhold_forecast": bool(applied.get("withhold_forecast", False)),
        "degrade_confidence": bool(applied.get("degrade_confidence", False)),
        "data_gap": int(counted.get("gaps_detected", 0)) > 0,
        "gap_count": int(counted.get("gaps_detected", 0)),
        "missing_candles_total": int(counted.get("missing_candles_total", 0)),
        "filled_candles": int(counted.get("filled_candles", 0)),
        "silent_fill_candidates": int(counted.get("silent_fill_candidates", 0)),
        "provenance_recorded": bool(counted.get("provenance_recorded", True)),
        "fill_count": actions.count(ACTION_FILL),
        "degrade_count": actions.count(ACTION_DEGRADE),
        "withhold_count": actions.count(ACTION_WITHHOLD),
    }


def build_reconciliation_report(
    primary: MarketDataLike,
    secondary: MarketDataLike,
    *,
    config: GapConfig | None = None,
    now: datetime | None = None,
    existing_provenance: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the auditable reconciliation report for the redundant providers."""
    cfg = config or GapConfig.from_env()
    checked = _normalized_now(now)
    primary_ts = [int(value) for value in primary.timestamps]
    secondary_ts = [int(value) for value in secondary.timestamps]
    secondary_set = set(secondary_ts)

    overlap = compare_redundant_overlap(primary, secondary, config=cfg)
    gaps = detect_missing_candles(primary_ts, interval_seconds=cfg.interval_seconds)
    secondary_gaps = detect_missing_candles(secondary_ts, interval_seconds=cfg.interval_seconds)

    decisions: list[dict[str, Any]] = []
    for run in gaps:
        covered = sum(1 for timestamp in run.timestamps if timestamp in secondary_set)
        decisions.append(
            decide_gap(
                first_timestamp=run.first_timestamp,
                missing_candles=run.missing_candles,
                covered_by_secondary=covered,
                overlap_status=str(overlap.get("status")),
                config=cfg,
            ).as_dict()
        )

    silent_fills = detect_silent_fills(primary, secondary, config=cfg)

    backfill = backfill_from_secondary(
        primary,
        secondary,
        config=cfg,
        existing_provenance=existing_provenance,
        now=checked,
    )
    idempotency = verify_backfill_idempotency(primary, secondary, config=cfg, now=checked)

    counts: dict[str, Any] = {
        "overlapping_candles": int(overlap.get("overlap_candles", 0)),
        "divergent_candles": len(overlap.get("divergent_candles", [])),
        "gaps_detected": len(gaps),
        "secondary_gaps_detected": len(secondary_gaps),
        "missing_candles_total": sum(int(run.missing_candles) for run in gaps),
        "silent_fill_candidates": len(silent_fills),
        "filled_candles": len(backfill.provenance),
        "provenance_recorded": True,
        "fill_gaps": sum(1 for item in decisions if item["action"] == ACTION_FILL),
        "degrade_gaps": sum(1 for item in decisions if item["action"] == ACTION_DEGRADE),
        "withhold_gaps": sum(1 for item in decisions if item["action"] == ACTION_WITHHOLD),
    }

    policy = apply_gap_policy(decisions)
    state = forecast_state(decisions, counts=counts, policy=policy)
    limit = cfg.report_gap_limit

    report: dict[str, Any] = {
        "schema_version": GAP_SCHEMA_VERSION,
        "generated_at": checked.isoformat(),
        "primary": {
            "source": "primary",
            "candles": len(primary_ts),
            "first_timestamp": min(primary_ts, default=None),
            "latest_timestamp": max(primary_ts, default=None),
        },
        "secondary": {
            "source": "secondary",
            "candles": len(secondary_ts),
            "first_timestamp": min(secondary_ts, default=None),
            "latest_timestamp": max(secondary_ts, default=None),
        },
        "overlap_comparison": overlap,
        "gaps": decisions[:limit],
        "secondary_gaps": [run.as_dict() for run in secondary_gaps][:limit],
        "silent_fills": silent_fills[:limit],
        "backfill": {
            "filled_candles": len(backfill.provenance),
            "provenance": backfill.provenance[:limit],
        },
        "idempotency": idempotency,
        "summary": counts,
        "policy": policy,
        "forecast_state": state,
        "config": cfg.as_dict(),
    }
    return report


def persist_reconciliation_report(report: dict[str, Any], path: Path = GAP_REPORT_PATH) -> None:
    """Publish the reconciliation report as a machine-readable JSON artifact."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
