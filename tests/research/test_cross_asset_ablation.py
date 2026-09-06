from __future__ import annotations

import unittest

from btc_timesfm.research.cross_asset_ablation import eligible_training_rows, evaluate_rows


def _row(index: int) -> dict:
    target = ((index % 11) - 5) * 0.05
    return {
        "origin_at": f"2026-01-{1 + index // 24:02d}T{index % 24:02d}:00:00+00:00",
        "origin_s": index * 3600,
        "regime": "range" if index % 2 else "trending",
        "base": [float(index % 7), float(index % 5), 0.1, 50.0, 0.2, 0.3, 0.4],
        "cross": [
            target,
            target * 0.8,
            target * 0.6,
            target * 0.4,
            target * 0.3,
            0.7,
            0.6,
            20.0 + index % 3,
            1.0,
            4.0,
            2.0,
        ],
        "targets": {
            "2h": target,
            "4h": target * 1.1,
            "8h": target * 1.2,
            "16h": target * 1.3,
        },
    }


class CrossAssetAblationTests(unittest.TestCase):
    def test_training_target_must_be_observable(self) -> None:
        rows = [_row(index) for index in range(10)]
        eligible = eligible_training_rows(rows, current_origin_s=7 * 3600, horizon=4)
        self.assertEqual([row["origin_s"] for row in eligible], [0, 3600, 7200, 10800])

    def test_report_is_deterministic_and_segmented(self) -> None:
        rows = [_row(index) for index in range(64)]
        first = evaluate_rows(rows, min_train=8)
        second = evaluate_rows(rows, min_train=8)
        self.assertEqual(first, second)
        self.assertEqual(set(first["horizons"]), {"2h", "4h", "8h", "16h"})
        for item in first["horizons"].values():
            self.assertGreater(item["walk_forward_samples"], 0)
            self.assertIn("significance", item)
            self.assertEqual(set(item["regimes"]), {"range", "trending"})

    def test_sparse_evidence_is_not_promoted(self) -> None:
        report = evaluate_rows([_row(index) for index in range(5)], min_train=16)
        for item in report["horizons"].values():
            self.assertEqual(item["walk_forward_samples"], 0)
            self.assertEqual(item["recommendation"], "insufficient_evidence")


if __name__ == "__main__":
    unittest.main()
