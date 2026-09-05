"""Deterministic purged walk-forward cross-validation for time-series research.

The splitter operates on forecast-origin timestamps rather than row numbers. A
training origin is eligible only when its longest target has matured before the
validation boundary, plus any configured embargo. This makes leakage prevention
explicit even when research origins are sparsely sampled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

import numpy as np


DEFAULT_CV_FOLDS = 3
DEFAULT_MIN_TRAIN_SAMPLES = 12
DEFAULT_PURGE_HOURS = 16
DEFAULT_EMBARGO_HOURS = 0
DEFAULT_ROLLING_TRAIN_SAMPLES = 24
VALID_MODES = ("expanding", "rolling")


@dataclass(frozen=True)
class WalkForwardFold:
    """One chronological train/validation split over forecast origins."""

    fold: int
    mode: str
    train_indices: tuple[int, ...]
    validation_indices: tuple[int, ...]
    purge_hours: int
    embargo_hours: int


def _iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _validate_timestamps(origin_timestamps: Sequence[int]) -> list[int]:
    timestamps = [int(value) for value in origin_timestamps]
    if len(timestamps) < 2:
        raise ValueError("cross-validation requires at least two forecast origins")
    if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
        raise ValueError("forecast-origin timestamps must be strictly increasing")
    return timestamps


def build_purged_walk_forward_folds(
    origin_timestamps: Sequence[int],
    *,
    folds: int = DEFAULT_CV_FOLDS,
    min_train_samples: int = DEFAULT_MIN_TRAIN_SAMPLES,
    purge_hours: int = DEFAULT_PURGE_HOURS,
    embargo_hours: int = DEFAULT_EMBARGO_HOURS,
    mode: str = "expanding",
    rolling_train_samples: int = DEFAULT_ROLLING_TRAIN_SAMPLES,
    max_target_hours: int = DEFAULT_PURGE_HOURS,
) -> list[WalkForwardFold]:
    """Build deterministic chronological folds with purge and embargo gaps.

    ``purge_hours`` must cover the longest scored target. A training origin at
    time ``t`` is allowed only when ``t + purge_hours`` is no later than the
    validation start minus ``embargo_hours``. This prevents target-label overlap
    with the validation boundary. Rolling mode additionally caps the number of
    retained training origins.
    """
    timestamps = _validate_timestamps(origin_timestamps)
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {VALID_MODES}")
    if folds < 1:
        raise ValueError("folds must be at least 1")
    if min_train_samples < 1 or min_train_samples >= len(timestamps):
        raise ValueError("min_train_samples must leave at least one validation origin")
    if purge_hours < max_target_hours:
        raise ValueError(
            f"purge_hours must be >= max_target_hours ({max_target_hours}) to prevent leakage"
        )
    if embargo_hours < 0:
        raise ValueError("embargo_hours cannot be negative")
    if mode == "rolling" and rolling_train_samples < 1:
        raise ValueError("rolling_train_samples must be at least 1")

    validation_candidates = np.arange(min_train_samples, len(timestamps), dtype=int)
    chunks = np.array_split(validation_candidates, min(folds, len(validation_candidates)))
    result: list[WalkForwardFold] = []

    for number, chunk in enumerate(chunks, start=1):
        if not len(chunk):
            continue
        validation_indices = tuple(map(int, chunk))
        validation_start = timestamps[validation_indices[0]]
        cutoff = validation_start - (purge_hours + embargo_hours) * 3600
        eligible = [index for index in range(validation_indices[0]) if timestamps[index] <= cutoff]
        if mode == "rolling":
            eligible = eligible[-rolling_train_samples:]
        if not eligible:
            raise ValueError(
                "purge/embargo settings leave a fold without training samples; "
                "increase the history window or reduce the validation start"
            )

        result.append(
            WalkForwardFold(
                fold=number,
                mode=mode,
                train_indices=tuple(eligible),
                validation_indices=validation_indices,
                purge_hours=purge_hours,
                embargo_hours=embargo_hours,
            )
        )

    return result


def assert_no_fold_leakage(
    fold: WalkForwardFold,
    origin_timestamps: Sequence[int],
    *,
    max_target_hours: int = DEFAULT_PURGE_HOURS,
) -> None:
    """Raise when a fold can expose a validation origin to a future training label."""
    timestamps = _validate_timestamps(origin_timestamps)
    validation_start_index = fold.validation_indices[0]
    validation_start = timestamps[validation_start_index]
    label_cutoff = validation_start - fold.embargo_hours * 3600

    if any(index >= validation_start_index for index in fold.train_indices):
        raise AssertionError("training indices must precede validation indices")
    for index in fold.train_indices:
        target_matures_at = timestamps[index] + max_target_hours * 3600
        if target_matures_at > label_cutoff:
            raise AssertionError(
                "training target overlaps validation boundary: "
                f"train={index} target={_iso(target_matures_at)} "
                f"validation={_iso(validation_start)}"
            )


def fold_definition(fold: WalkForwardFold, origin_timestamps: Sequence[int]) -> dict[str, Any]:
    """Return the exact, JSON-serializable fold definition for experiment manifests."""
    timestamps = _validate_timestamps(origin_timestamps)
    train = list(fold.train_indices)
    validation = list(fold.validation_indices)
    return {
        "fold": fold.fold,
        "mode": fold.mode,
        "purge_hours": fold.purge_hours,
        "embargo_hours": fold.embargo_hours,
        "train_samples": len(train),
        "validation_samples": len(validation),
        "train_indices": train,
        "validation_indices": validation,
        "train_start_at": _iso(timestamps[train[0]]),
        "train_end_at": _iso(timestamps[train[-1]]),
        "validation_start_at": _iso(timestamps[validation[0]]),
        "validation_end_at": _iso(timestamps[validation[-1]]),
    }
