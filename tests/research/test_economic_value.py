import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from btc_timesfm.history.history_store import ForecastHistoryStore
from btc_timesfm.research.economic_value import (
    DEFAULT_ASSUMPTIONS,
    build_report,
    generate_report,
    render_markdown,
)


NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def row(
    *,
    origin_at: str,
    horizon: int,
    predicted_change: float,
    actual_change: float,
    origin_price: float = 100.0,
    target_price: float = 102.0,
    model: str = "ensemble",
    regime: str = "range",
) -> dict[str, object]:
    return {
        "origin_at": origin_at,
        "target_at": origin_at,
        "model_name": model,
        "horizon_hours": horizon,
        "regime": regime,
        "actual_target_price_usd": target_price,
        "source_price_usd": origin_price,
        "predicted_change_pct": predicted_change,
        "actual_change_pct": actual_change,
    }


class EconomicValueTests(unittest.TestCase):
    def test_configurable_assumptions_appear_in_report(self) -> None:
        custom = {"round_trip_fee_pct": 0.2, "slippage_pct": 0.1}
        report = build_report([], now=NOW, assumptions=custom, bootstrap_iterations=100)
        self.assertEqual(report["assumptions"]["round_trip_fee_pct"], 0.2)
        self.assertEqual(report["assumptions"]["slippage_pct"], 0.1)
        # Defaults preserved for unspecified keys
        self.assertEqual(
            report["assumptions"]["decision_threshold_pct"],
            DEFAULT_ASSUMPTIONS["decision_threshold_pct"],
        )

    def test_only_matured_out_of_sample_rows_evaluated(self) -> None:
        matured = row(
            origin_at="2026-09-07T00:00:00+00:00",
            horizon=2,
            predicted_change=1.0,
            actual_change=2.0,
            target_price=102.0,
        )
        unmatured = dict(matured)
        unmatured["actual_target_price_usd"] = None
        report = build_report(
            [matured, unmatured], now=NOW, low_sample_threshold=1, bootstrap_iterations=100
        )
        self.assertEqual(report["evaluated_samples"], 1)
        self.assertEqual(report["matured_rows"], 1)
        self.assertTrue(report["leakage_guard"]["uses_only_matured_outcomes"])
        self.assertTrue(report["leakage_guard"]["strictly_out_of_sample"])

    def test_results_separated_by_horizon(self) -> None:
        samples = [
            row(
                origin_at="2026-09-07T00:00:00+00:00",
                horizon=2,
                predicted_change=1.0,
                actual_change=1.5,
            ),
            row(
                origin_at="2026-09-07T00:00:00+00:00",
                horizon=4,
                predicted_change=-1.0,
                actual_change=-0.5,
                target_price=100.5,
            ),
            row(
                origin_at="2026-09-07T08:00:00+00:00",
                horizon=8,
                predicted_change=0.05,
                actual_change=0.1,
            ),
            row(
                origin_at="2026-09-07T00:00:00+00:00",
                horizon=16,
                predicted_change=3.0,
                actual_change=4.0,
                target_price=104.0,
            ),
        ]
        report = build_report(samples, now=NOW, low_sample_threshold=1, bootstrap_iterations=100)
        self.assertIn("2h", report["by_horizon"])
        self.assertIn("4h", report["by_horizon"])
        self.assertIn("8h", report["by_horizon"])
        self.assertIn("16h", report["by_horizon"])
        self.assertEqual(report["by_horizon"]["2h"]["samples"], 1)
        self.assertEqual(report["by_horizon"]["4h"]["samples"], 1)

    def test_turnover_reported(self) -> None:
        samples = [
            row(
                origin_at="2026-09-07T00:00:00+00:00",
                horizon=2,
                predicted_change=1.0,
                actual_change=1.5,
            ),
            row(
                origin_at="2026-09-07T02:00:00+00:00",
                horizon=2,
                predicted_change=0.05,
                actual_change=0.1,
            ),
        ]
        report = build_report(samples, now=NOW, low_sample_threshold=1, bootstrap_iterations=100)
        turnover = report["overall_turnover"]
        self.assertEqual(turnover["total_samples"], 2)
        self.assertEqual(turnover["active_signal_samples"], 1)
        self.assertAlmostEqual(turnover["turnover_rate"], 0.5, places=4)
        # Per-horizon turnover also reported
        self.assertIn("turnover", report["by_horizon"]["2h"])

    def test_no_execution_or_trading_functionality(self) -> None:
        report = build_report([], now=NOW, bootstrap_iterations=100)
        self.assertTrue(report["leakage_guard"]["no_execution_or_trading"])

    def test_practically_negligible_flagged(self) -> None:
        # Create enough samples where statistical significance can be established
        # but friction-adjusted effect is tiny
        samples = []
        for i in range(40):
            samples.append(
                row(
                    origin_at=f"2026-09-07T{i:02d}:00:00+00:00",
                    horizon=2,
                    predicted_change=0.2,
                    actual_change=0.15,
                    target_price=100.15,
                )
            )
        report = build_report(
            samples,
            now=NOW,
            low_sample_threshold=1,
            min_paired_samples=1,
            bootstrap_iterations=100,
            assumptions={"min_meaningful_move_pct": 0.5},
        )
        # With tiny returns and friction, friction-adjusted effect should be negative
        two_hour = report["by_horizon"]["2h"]
        self.assertIsNotNone(two_hour.get("practically_negligible"))
        self.assertIn("2h", report["by_horizon"])

    def test_assumptions_validate(self) -> None:
        with self.assertRaises(ValueError):
            build_report([], low_sample_threshold=0)
        with self.assertRaises(ValueError):
            build_report([], min_paired_samples=0)
        with self.assertRaises(ValueError):
            build_report([], bootstrap_iterations=99)

    def test_markdown_contains_assumptions_and_horizons(self) -> None:
        report = build_report([], now=NOW, bootstrap_iterations=100)
        md = render_markdown(report)
        self.assertIn("Economic-value evaluation", md)
        self.assertIn("Round-trip fee", md)
        self.assertIn("Slippage", md)
        self.assertIn("research-only", md)
        self.assertIn("2h", md)

    def test_generate_report_reads_durable_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "history.sqlite"
            store = ForecastHistoryStore(db)
            snapshot = {
                "generated_at": "2026-09-07T00:00:00+00:00",
                "latest_close_at": "2026-09-07T00:00:00+00:00",
                "latest_close_usd": 100.0,
                "source": "test",
                "pair": "BTCUSD",
                "regime": "range",
                "market_features": {"volatility_24h_pct": 0.5},
                "predictions": {"2h": {"price_usd": 102.0, "change_pct": 2.0}},
                "model_predictions": {"persistence": {"2h": {"price_usd": 100.0}}},
                "model_weights": {"2h": {"persistence": 0.2}},
            }
            target = int(datetime(2026, 9, 7, 2, tzinfo=timezone.utc).timestamp())
            store.ingest_snapshot(snapshot, {target: 101.0})

            report = generate_report(
                db,
                json_path=root / "econ.json",
                markdown_path=root / "econ.md",
                low_sample_threshold=1,
                min_paired_samples=1,
                bootstrap_iterations=100,
            )

            self.assertEqual(report["evaluated_samples"], 1)
            self.assertTrue((root / "econ.json").exists())
            self.assertTrue((root / "econ.md").exists())

    def test_model_filter_only_evaluates_specified_model(self) -> None:
        samples = [
            row(
                origin_at="2026-09-07T00:00:00+00:00",
                horizon=2,
                predicted_change=1.0,
                actual_change=1.5,
                model="ensemble",
            ),
            row(
                origin_at="2026-09-07T00:00:00+00:00",
                horizon=2,
                predicted_change=0.5,
                actual_change=1.5,
                model="persistence",
            ),
        ]
        report = build_report(
            samples,
            now=NOW,
            model_name="ensemble",
            low_sample_threshold=1,
            bootstrap_iterations=100,
        )
        self.assertEqual(report["evaluated_samples"], 1)


if __name__ == "__main__":
    unittest.main()
