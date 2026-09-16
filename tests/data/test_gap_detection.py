#!/usr/bin/env python3
"""Unit tests for upstream market-data gap detection, reconciliation and backfill."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from btc_timesfm.data.gap_detection import (
    ACTION_DEGRADE,
    ACTION_FILL,
    ACTION_WITHHOLD,
    CLASSIFICATION_FILLED_BY_SECONDARY,
    CLASSIFICATION_MARKED_IN_HISTORY,
    CLASSIFICATION_REQUIRES_WITHHOLDING,
    GAP_SCHEMA_VERSION,
    GapConfig,
    apply_gap_policy,
    backfill_from_secondary,
    build_reconciliation_report,
    compare_redundant_overlap,
    decide_gap,
    detect_missing_candles,
    detect_silent_fills,
    forecast_state,
    gap_policy_rules,
    persist_reconciliation_report,
    verify_backfill_idempotency,
)


NOW = datetime(2026, 9, 6, 18, 0, 0, tzinfo=timezone.utc)

DETAIL_CONFIG = GapConfig(
    interval_seconds=3600,
    allowed_missing_candles=1,
    fill_max_gap_candles=3,
    comparison_candles=24,
    min_overlap_candles=6,
    divergence_threshold_pct=0.75,
    report_gap_limit=200,
)


@dataclass
class OHLCV:
    timestamps: list[int]
    opens: np.ndarray
    highs: np.ndarray
    lows: np.ndarray
    closes: np.ndarray
    volumes: np.ndarray


def _ts(*, relative_hours: int) -> int:
    return int((NOW - timedelta(hours=relative_hours)).timestamp())


def make_series(
    *,
    count: int = 80,
    end_at=NOW,
    remove: set[int] | None = None,
) -> OHLCV:
    first = end_at - timedelta(hours=count - 1)
    timestamps = [int((first + timedelta(hours=i)).timestamp()) for i in range(count)]
    closes = np.linspace(100_000.0, 101_000.0, count, dtype=np.float32)
    removed = {int(v) for v in (remove or set())}
    keep = [index for index, timestamp in enumerate(timestamps) if timestamp not in removed]
    return OHLCV(
        timestamps=[timestamps[index] for index in keep],
        opens=closes[keep] - 20.0,
        highs=closes[keep] + 80.0,
        lows=closes[keep] - 80.0,
        closes=closes[keep],
        volumes=np.linspace(100.0, 120.0, count, dtype=np.float32)[keep],
    )


class DetectMissingCandlesTests(unittest.TestCase):
    def test_no_gaps(self) -> None:
        series = make_series()
        runs = detect_missing_candles(
            series.timestamps, interval_seconds=DETAIL_CONFIG.interval_seconds
        )
        self.assertEqual(runs, [])

    def test_single_gap(self) -> None:
        primary = make_series(remove={_ts(relative_hours=40)})
        runs = detect_missing_candles(
            primary.timestamps, interval_seconds=DETAIL_CONFIG.interval_seconds
        )
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].missing_candles, 1)
        self.assertEqual(runs[0].gap_seconds, 3600)
        self.assertIn(_ts(relative_hours=40), runs[0].timestamps)

    def test_multi_candle_gap_grouped(self) -> None:
        primary = make_series(remove={_ts(relative_hours=40), _ts(relative_hours=41)})
        runs = detect_missing_candles(
            primary.timestamps, interval_seconds=DETAIL_CONFIG.interval_seconds
        )
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0].missing_candles, 2)
        self.assertEqual(runs[0].gap_seconds, 7200)


class CompareRedundantOverlapTests(unittest.TestCase):
    def test_aligned_series_report_ok(self) -> None:
        primary = make_series()
        secondary = make_series(count=80, end_at=NOW)
        report = compare_redundant_overlap(primary, secondary, config=DETAIL_CONFIG)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["overlap_candles"], 24)
        self.assertIsInstance(report["max_close_difference_pct"], float)

    def test_insufficient_overlap(self) -> None:
        primary = make_series(
            count=80, end_at=NOW, remove={_ts(relative_hours=i) for i in range(75)}
        )
        secondary = make_series(
            count=80, end_at=NOW, remove={_ts(relative_hours=i) for i in range(75)}
        )
        report = compare_redundant_overlap(primary, secondary, config=DETAIL_CONFIG)
        self.assertEqual(report["status"], "insufficient_overlap")
        self.assertLessEqual(report["overlap_candles"], DETAIL_CONFIG.min_overlap_candles)


class DecideGapTests(unittest.TestCase):
    def test_fill_when_secondary_covers(self) -> None:
        decision = decide_gap(
            first_timestamp=1,
            missing_candles=1,
            covered_by_secondary=1,
            overlap_status="ok",
            config=DETAIL_CONFIG,
        )
        self.assertEqual(decision.action, ACTION_FILL)
        self.assertEqual(decision.classification, CLASSIFICATION_FILLED_BY_SECONDARY)
        self.assertEqual(decision.rule_id, "fill-if-covered")

    def test_degrade_when_divergence_detected(self) -> None:
        decision = decide_gap(
            first_timestamp=1,
            missing_candles=1,
            covered_by_secondary=1,
            overlap_status="disagreement",
            config=DETAIL_CONFIG,
        )
        self.assertEqual(decision.action, ACTION_DEGRADE)
        self.assertEqual(decision.classification, CLASSIFICATION_MARKED_IN_HISTORY)
        self.assertEqual(decision.rule_id, "fill-with-mark-degrade-if-mismatch")

    def test_degrade_for_minor_uncovered_gap(self) -> None:
        decision = decide_gap(
            first_timestamp=1,
            missing_candles=DETAIL_CONFIG.allowed_missing_candles,
            covered_by_secondary=0,
            overlap_status="ok",
            config=DETAIL_CONFIG,
        )
        self.assertEqual(decision.action, ACTION_DEGRADE)
        self.assertEqual(decision.classification, CLASSIFICATION_MARKED_IN_HISTORY)

    def test_withhold_for_large_gap(self) -> None:
        decision = decide_gap(
            first_timestamp=1,
            missing_candles=DETAIL_CONFIG.fill_max_gap_candles + 1,
            covered_by_secondary=DETAIL_CONFIG.fill_max_gap_candles + 1,
            overlap_status="ok",
            config=DETAIL_CONFIG,
        )
        self.assertEqual(decision.action, ACTION_WITHHOLD)
        self.assertEqual(decision.classification, CLASSIFICATION_REQUIRES_WITHHOLDING)

    def test_withhold_for_uncovered_gap_above_allowed(self) -> None:
        decision = decide_gap(
            first_timestamp=1,
            missing_candles=DETAIL_CONFIG.allowed_missing_candles + 1,
            covered_by_secondary=0,
            overlap_status="ok",
            config=DETAIL_CONFIG,
        )
        self.assertEqual(decision.action, ACTION_WITHHOLD)
        self.assertEqual(decision.classification, CLASSIFICATION_REQUIRES_WITHHOLDING)

    def test_all_rules_are_represented(self) -> None:
        rule_ids = {rule["rule_id"] for rule in gap_policy_rules()}
        self.assertIn("fill-if-covered", rule_ids)
        self.assertIn("fill-with-mark-degrade-if-mismatch", rule_ids)
        self.assertIn("withhold-if-uncovered", rule_ids)


class PolicyTests(unittest.TestCase):
    def test_fill_policy_when_all_fill(self) -> None:
        result = apply_gap_policy(
            [
                {"action": ACTION_FILL},
                {"action": ACTION_FILL},
            ]
        )
        self.assertEqual(result["policy_applied"], ACTION_FILL)
        self.assertFalse(result["withhold_forecast"])
        self.assertTrue(result["filled_any"])

    def test_withhold_overrides_fill(self) -> None:
        result = apply_gap_policy(
            [
                {"action": ACTION_FILL},
                {"action": ACTION_WITHHOLD},
            ]
        )
        self.assertEqual(result["policy_applied"], ACTION_WITHHOLD)
        self.assertTrue(result["withhold_forecast"])

    def test_degrade_when_no_withhold(self) -> None:
        result = apply_gap_policy([{"action": ACTION_DEGRADE}])
        self.assertEqual(result["policy_applied"], ACTION_DEGRADE)
        self.assertTrue(result["degrade_confidence"])

    def test_none_when_no_gaps(self) -> None:
        result = apply_gap_policy([])
        self.assertEqual(result["policy_applied"], "none")

    def test_rules_are_machine_readable(self) -> None:
        rules = gap_policy_rules()
        self.assertGreater(len(rules), 0)
        for rule in rules:
            self.assertIn("rule_id", rule)
            self.assertIn("action", rule)
            self.assertIn("classification", rule)
            self.assertIn("condition", rule)
            self.assertIn("reason", rule)


class BackfillTests(unittest.TestCase):
    def test_covers_single_gap(self) -> None:
        gap_ts = _ts(relative_hours=40)
        primary = make_series(remove={gap_ts})
        secondary = make_series()
        result = backfill_from_secondary(primary, secondary, config=DETAIL_CONFIG, now=NOW)
        self.assertIn(gap_ts, result.timestamps)
        self.assertEqual(len(result.provenance), 1)
        self.assertEqual(result.provenance[0]["timestamp"], gap_ts)
        self.assertEqual(result.provenance[0]["source"], "secondary")
        self.assertEqual(result.provenance[0]["rule_id"], "fill-if-covered")

    def test_no_fill_when_gap_too_large(self) -> None:
        primary = make_series(
            remove={
                _ts(relative_hours=40),
                _ts(relative_hours=41),
                _ts(relative_hours=42),
                _ts(relative_hours=43),
            }
        )
        secondary = make_series()
        result = backfill_from_secondary(primary, secondary, config=DETAIL_CONFIG, now=NOW)
        self.assertEqual(len(result.provenance), 0)

    def test_backfill_preserves_original_candles(self) -> None:
        primary = make_series()
        secondary = make_series()
        result = backfill_from_secondary(primary, secondary, config=DETAIL_CONFIG, now=NOW)
        self.assertEqual(result.timestamps, primary.timestamps)
        self.assertEqual(len(result.provenance), 0)


class IdempotencyTests(unittest.TestCase):
    def test_single_gap_idempotent(self) -> None:
        gap_ts = _ts(relative_hours=40)
        primary = make_series(remove={gap_ts})
        secondary = make_series()
        report = verify_backfill_idempotency(primary, secondary, config=DETAIL_CONFIG, now=NOW)
        self.assertTrue(report["idempotent"])
        self.assertTrue(report["identical_timestamps"])
        self.assertTrue(report["identical_ohlcv"])
        self.assertEqual(report["remaining_gaps"], 0)
        self.assertEqual(report["filled_candles"], 1)
        self.assertTrue(report["provenance_stable"])

    def test_no_gap_idempotent(self) -> None:
        primary = make_series()
        secondary = make_series()
        report = verify_backfill_idempotency(primary, secondary, config=DETAIL_CONFIG, now=NOW)
        self.assertTrue(report["idempotent"])
        self.assertEqual(report["filled_candles"], 0)


class SilentFillsTests(unittest.TestCase):
    def test_gap_covered_candle_detected(self) -> None:
        gap_ts = _ts(relative_hours=40)
        primary = make_series(remove={gap_ts})
        secondary = make_series()
        fills = detect_silent_fills(primary, secondary, config=DETAIL_CONFIG)
        gap_fills = [item for item in fills if item["timestamp"] == gap_ts]
        self.assertEqual(len(gap_fills), 1)
        self.assertEqual(gap_fills[0]["kind"], "gap_fill_candidate")

    def test_off_lattice_secondary_detected(self) -> None:
        primary = make_series(
            count=80, end_at=NOW, remove={_ts(relative_hours=40), _ts(relative_hours=41)}
        )
        secondary = make_series(count=80, end_at=NOW, remove={_ts(relative_hours=40)})
        off_lattice_gap_41 = _ts(relative_hours=41) + 600
        secondary.timestamps = sorted(set(secondary.timestamps) | {off_lattice_gap_41})
        fills = detect_silent_fills(primary, secondary, config=DETAIL_CONFIG)
        self.assertTrue(any(item["kind"] == "off_lattice_candle" for item in fills))
        self.assertTrue(any(item["kind"] == "gap_fill_candidate" for item in fills))


class ForecastStateTests(unittest.TestCase):
    def test_none_policy_has_state(self) -> None:
        state = forecast_state([], counts={"gaps_detected": 0, "missing_candles_total": 0})
        self.assertFalse(state["withhold_forecast"])
        self.assertFalse(state["data_gap"])
        self.assertEqual(state["action"], "none")

    def test_withhold_state(self) -> None:
        state = forecast_state(
            [{"action": ACTION_WITHHOLD, "first_timestamp": 1, "missing_candles": 5}],
            counts={"gaps_detected": 1, "missing_candles_total": 5},
        )
        self.assertTrue(state["withhold_forecast"])
        self.assertTrue(state["data_gap"])


class BuildReconciliationReportTests(unittest.TestCase):
    def test_clean_report_shape(self) -> None:
        primary = make_series()
        secondary = make_series()
        report = build_reconciliation_report(primary, secondary, config=DETAIL_CONFIG, now=NOW)
        self.assertEqual(report["schema_version"], GAP_SCHEMA_VERSION)
        self.assertIn("primary", report)
        self.assertIn("secondary", report)
        self.assertIn("overlap_comparison", report)
        self.assertIn("gaps", report)
        self.assertIn("silent_fills", report)
        self.assertIn("backfill", report)
        self.assertIn("idempotency", report)
        self.assertIn("summary", report)
        self.assertIn("policy", report)
        self.assertIn("forecast_state", report)
        self.assertEqual(report["summary"]["gaps_detected"], 0)
        self.assertEqual(report["summary"]["withhold_gaps"], 0)

    def test_report_lists_single_gap_decision(self) -> None:
        gap_ts = _ts(relative_hours=40)
        primary = make_series(remove={gap_ts})
        secondary = make_series()
        report = build_reconciliation_report(primary, secondary, config=DETAIL_CONFIG, now=NOW)
        self.assertEqual(report["summary"]["gaps_detected"], 1)
        self.assertEqual(len(report["gaps"]), 1)
        self.assertEqual(report["gaps"][0]["missing_candles"], 1)
        self.assertEqual(report["gaps"][0]["covered_by_secondary"], 1)

    def test_report_withholds_for_large_gap(self) -> None:
        gap_remove = {
            _ts(relative_hours=40),
            _ts(relative_hours=41),
            _ts(relative_hours=42),
            _ts(relative_hours=43),
        }
        primary = make_series(remove=gap_remove)
        secondary = make_series()
        report = build_reconciliation_report(primary, secondary, config=DETAIL_CONFIG, now=NOW)
        self.assertEqual(report["summary"]["withhold_gaps"], 1)
        self.assertTrue(report["policy"]["withhold_forecast"])
        self.assertTrue(report["forecast_state"]["withhold_forecast"])


class PersistTests(unittest.TestCase):
    def test_persist_report_writes_json(self) -> None:
        report = {"schema_version": GAP_SCHEMA_VERSION, "summary": {}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gap_reconciliation.json"
            persist_reconciliation_report(report, path=path)
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["schema_version"], GAP_SCHEMA_VERSION)
            self.assertIn("summary", loaded)


class ConfigTests(unittest.TestCase):
    def test_invalid_fill_max_below_allowed_raises(self) -> None:
        with self.assertRaises(ValueError):
            GapConfig(allowed_missing_candles=2, fill_max_gap_candles=1)

    def test_negative_threshold_raises(self) -> None:
        with self.assertRaises(ValueError):
            GapConfig(divergence_threshold_pct=-0.5)


if __name__ == "__main__":
    unittest.main()
