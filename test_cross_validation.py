#!/usr/bin/env python3
"""Unit tests for purged walk-forward cross-validation."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from cross_validation import (
    assert_no_fold_leakage,
    build_purged_walk_forward_folds,
    fold_definition,
)


def hourly_timestamps(count: int) -> list[int]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [int((start + timedelta(hours=hour)).timestamp()) for hour in range(count)]


class CrossValidationTests(unittest.TestCase):
    def test_fold_definitions_are_deterministic(self) -> None:
        timestamps = hourly_timestamps(80)
        kwargs = {
            "folds": 3,
            "min_train_samples": 32,
            "purge_hours": 16,
            "embargo_hours": 2,
            "mode": "expanding",
            "max_target_hours": 16,
        }
        first = build_purged_walk_forward_folds(timestamps, **kwargs)
        second = build_purged_walk_forward_folds(timestamps, **kwargs)
        self.assertEqual(first, second)
        self.assertEqual(
            [fold_definition(fold, timestamps) for fold in first],
            [fold_definition(fold, timestamps) for fold in second],
        )

    def test_purge_and_embargo_prevent_label_overlap(self) -> None:
        timestamps = hourly_timestamps(96)
        folds = build_purged_walk_forward_folds(
            timestamps,
            folds=4,
            min_train_samples=40,
            purge_hours=16,
            embargo_hours=3,
            max_target_hours=16,
        )
        for fold in folds:
            assert_no_fold_leakage(fold, timestamps, max_target_hours=16)
            validation_start = timestamps[fold.validation_indices[0]]
            for index in fold.train_indices:
                self.assertLessEqual(
                    timestamps[index] + 16 * 3600,
                    validation_start - 3 * 3600,
                )

    def test_expanding_training_history_only_grows(self) -> None:
        timestamps = hourly_timestamps(100)
        folds = build_purged_walk_forward_folds(
            timestamps,
            folds=4,
            min_train_samples=40,
            purge_hours=16,
            mode="expanding",
        )
        train_sizes = [len(fold.train_indices) for fold in folds]
        self.assertEqual(train_sizes, sorted(train_sizes))
        self.assertTrue(all(size > 0 for size in train_sizes))

    def test_rolling_training_history_is_bounded(self) -> None:
        timestamps = hourly_timestamps(100)
        folds = build_purged_walk_forward_folds(
            timestamps,
            folds=4,
            min_train_samples=40,
            purge_hours=16,
            mode="rolling",
            rolling_train_samples=10,
        )
        self.assertTrue(folds)
        for fold in folds:
            self.assertLessEqual(len(fold.train_indices), 10)
            assert_no_fold_leakage(fold, timestamps, max_target_hours=16)

    def test_purge_shorter_than_longest_target_is_rejected(self) -> None:
        timestamps = hourly_timestamps(50)
        with self.assertRaisesRegex(ValueError, "purge_hours"):
            build_purged_walk_forward_folds(
                timestamps,
                folds=2,
                min_train_samples=20,
                purge_hours=8,
                max_target_hours=16,
            )

    def test_manifest_definition_preserves_exact_indices_and_boundaries(self) -> None:
        timestamps = hourly_timestamps(60)
        fold = build_purged_walk_forward_folds(
            timestamps,
            folds=2,
            min_train_samples=30,
            purge_hours=16,
            embargo_hours=1,
        )[0]
        definition = fold_definition(fold, timestamps)
        self.assertEqual(definition["train_indices"], list(fold.train_indices))
        self.assertEqual(definition["validation_indices"], list(fold.validation_indices))
        self.assertEqual(definition["purge_hours"], 16)
        self.assertEqual(definition["embargo_hours"], 1)
        self.assertEqual(definition["train_samples"], len(fold.train_indices))
        self.assertEqual(definition["validation_samples"], len(fold.validation_indices))


if __name__ == "__main__":
    unittest.main()
