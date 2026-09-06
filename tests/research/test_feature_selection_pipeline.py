from __future__ import annotations

import unittest

from btc_timesfm.research.feature_selection_pipeline import (
    build_feature_selection_report,
    render_summary,
)


BASELINE = ["volatility_24h_pct", "range_24h_avg_pct", "volume_zscore_7d"]


def _report(name: str, *, recommendation: str, improvement: float, better_horizons: int) -> dict:
    horizons = {
        "2h": {
            "walk_forward_samples": 12,
            "baseline_mae_pp": 1.0,
            "candidate_mae_pp": 1.0 - improvement,
            "significance": {
                "conclusion": "candidate_better" if better_horizons else "inconclusive"
            },
        },
        "4h": {
            "walk_forward_samples": 12,
            "baseline_mae_pp": 1.2,
            "candidate_mae_pp": 1.2 - improvement,
            "significance": {
                "conclusion": "candidate_better" if better_horizons > 1 else "inconclusive"
            },
        },
    }
    return {
        "feature_names": [f"{name}_feature_a", f"{name}_feature_b"],
        "feature_sets": {"market_only": BASELINE},
        "horizons": horizons,
        "overall": {
            "mean_relative_mae_improvement": improvement,
            "no_material_horizon_regression": improvement >= -0.05,
            "statistically_better_horizons": better_horizons,
            "recommendation": recommendation,
        },
    }


class FeatureSelectionPipelineTests(unittest.TestCase):
    def test_builds_versioned_selection_from_component_reports(self) -> None:
        report = build_feature_selection_report(
            {
                "cross_asset": _report(
                    "cross_asset",
                    recommendation="edge_detected",
                    improvement=0.03,
                    better_horizons=2,
                ),
                "microstructure": _report(
                    "microstructure",
                    recommendation="insufficient_evidence",
                    improvement=0.0,
                    better_horizons=0,
                ),
            },
            generated_at="2026-01-05T00:00:00+00:00",
        )

        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["generated_at"], "2026-01-05T00:00:00+00:00")
        self.assertTrue(report["feature_set_version"].startswith("feature-set-"))
        self.assertEqual(report["selection"]["selected_groups"], ["cross_asset"])
        self.assertEqual(
            report["selection"]["selected_feature_names"],
            ["cross_asset_feature_a", "cross_asset_feature_b"],
        )
        self.assertEqual(report["component_count"], 2)
        self.assertIn("cross_asset", render_summary(report))

    def test_rejects_mismatched_baselines(self) -> None:
        report = _report(
            "cross_asset", recommendation="edge_detected", improvement=0.03, better_horizons=2
        )
        report["feature_sets"]["market_only"] = ["different"]
        with self.assertRaisesRegex(ValueError, "same baseline"):
            build_feature_selection_report(
                {
                    "cross_asset": _report(
                        "cross_asset",
                        recommendation="edge_detected",
                        improvement=0.03,
                        better_horizons=2,
                    ),
                    "microstructure": report,
                }
            )


if __name__ == "__main__":
    unittest.main()
