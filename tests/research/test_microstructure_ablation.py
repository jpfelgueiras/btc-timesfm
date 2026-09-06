from __future__ import annotations

import unittest

from btc_timesfm.research.microstructure_ablation import eligible_training_rows, evaluate_rows


def _row(index: int, horizon: int, *, target: float, micro_scale: float = 1.0) -> dict:
    origin_s = index * 3600
    return {
        "origin_at": f"2026-01-01T{index % 24:02d}:00:00+00:00",
        "origin_s": origin_s,
        "target_s": origin_s + horizon * 3600,
        "regime": "range" if index % 2 else "trend_up",
        "horizon": horizon,
        "base": [float(index % 7), float(index % 5), 0.1, 50.0, 0.2, 0.3, 0.4],
        "micro": [
            2.0,
            1_000_000.0,
            900_000.0,
            target * micro_scale,
            2_000_000.0,
            1_800_000.0,
            target * micro_scale,
            target * micro_scale,
        ],
        "target": target,
    }


class MicrostructureAblationTests(unittest.TestCase):
    def test_training_rows_require_target_to_have_matured(self) -> None:
        rows = [_row(index, 4, target=float(index) / 100.0) for index in range(10)]
        eligible = eligible_training_rows(rows, current_origin_s=7 * 3600, horizon=4)
        self.assertEqual([row["origin_s"] for row in eligible], [0, 3600, 7200, 10800])

    def test_report_is_horizon_specific_and_deterministic(self) -> None:
        rows = []
        for horizon in (2, 4, 8, 16):
            for index in range(48):
                target = ((index % 9) - 4) * 0.04
                rows.append(_row(index, horizon, target=target))

        first = evaluate_rows(rows, min_train=8)
        second = evaluate_rows(rows, min_train=8)
        self.assertEqual(first, second)
        self.assertEqual(set(first["horizons"]), {"2h", "4h", "8h", "16h"})
        for item in first["horizons"].values():
            self.assertGreater(item["walk_forward_samples"], 0)
            self.assertIn("significance", item)
            self.assertIn("regimes", item)

    def test_sparse_history_is_explicitly_insufficient(self) -> None:
        rows = [_row(index, 2, target=0.1) for index in range(5)]
        report = evaluate_rows(rows, min_train=24)
        item = report["horizons"]["2h"]
        self.assertEqual(item["walk_forward_samples"], 0)
        self.assertEqual(item["recommendation"], "insufficient_evidence")


if __name__ == "__main__":
    unittest.main()
