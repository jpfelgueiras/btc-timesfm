from datetime import datetime, timezone

from btc_timesfm.research.segment_evaluation import (
    Segment,
    SegmentEvaluationPolicy,
    evaluate_segments,
)


NOW = datetime(2026, 9, 16, tzinfo=timezone.utc)


def _row(origin: str, error: float, *, volatility: str = "high", quality: str = "clean") -> dict:
    return {
        "origin_at": origin,
        "target_at": origin.replace("T00:00:00", "T02:00:00"),
        "horizon_hours": 2,
        "absolute_error_pct": error,
        "segments": {
            "regime": "high_volatility",
            "volatility": volatility,
            "liquidity": "low",
            "data_quality": quality,
        },
    }


def test_segment_report_is_paired_reproducible_and_blocks_protected_degradation() -> None:
    origins = [f"2026-09-{day:02d}T00:00:00+00:00" for day in range(1, 9)]
    candidate = [_row(origin, 1.2) for origin in origins]
    baseline = [_row(origin, 1.0) for origin in origins]
    policy = SegmentEvaluationPolicy(minimum_paired_samples=8, bootstrap_iterations=100)
    first = evaluate_segments(candidate, baseline, now=NOW, policy=policy)
    second = evaluate_segments(candidate, baseline, now=NOW, policy=policy)
    assert first == second
    high_volatility = first["segments"]["volatility:high"]
    assert high_volatility["paired_samples"] == 8
    assert high_volatility["paired_evidence"]["conclusion"] == "baseline_better"
    assert first["promotion_guard"]["blocked_without_approval"]


def test_low_sample_is_explicit_and_never_blocks_promotion() -> None:
    report = evaluate_segments(
        [_row("2026-09-01T00:00:00+00:00", 2.0)],
        [_row("2026-09-01T00:00:00+00:00", 1.0)],
        now=NOW,
        segments=[Segment("volatility", "high", True)],
        policy=SegmentEvaluationPolicy(minimum_paired_samples=8, bootstrap_iterations=100),
    )
    segment = report["segments"]["volatility:high"]
    assert segment["low_sample"]
    assert segment["low_sample_reason"] == "paired_samples:1<8"
    assert not report["promotion_guard"]["blocked_without_approval"]


def test_unmatured_and_naive_timestamps_are_excluded() -> None:
    report = evaluate_segments(
        [_row("2026-09-01T00:00:00", 1.0), _row("2026-09-20T00:00:00+00:00", 1.0)],
        [_row("2026-09-01T00:00:00", 1.0), _row("2026-09-20T00:00:00+00:00", 1.0)],
        now=NOW,
        segments=[Segment("volatility", "high", True)],
        policy=SegmentEvaluationPolicy(bootstrap_iterations=100),
    )
    assert report["input"]["paired_rows"] == 0
    assert report["input"]["candidate_excluded"]["invalid_or_untimestamped_row"] == 1
