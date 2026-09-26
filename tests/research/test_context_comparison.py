from __future__ import annotations

import unittest

from btc_timesfm.research.context_comparison import (
    CONTEXT_LENGTHS,
    assess_candidate,
    build_report,
    candidate_catalog,
    validate_origin_pairing,
)


class ContextComparisonTests(unittest.TestCase):
    def test_catalog_is_bounded_and_production_identity_is_pinned(self) -> None:
        production = candidate_catalog()[0]
        self.assertEqual(production.supported_contexts, CONTEXT_LENGTHS)
        self.assertEqual(production.revision, "43046b85ec22d584a13f8098c2ed39c889e129c2")
        self.assertFalse(candidate_catalog()[1].authorized)

    def test_physical_lookback_counts_closes_not_only_returns(self) -> None:
        result = assess_candidate(candidate_catalog()[0], available_returns=336)
        self.assertEqual(result["supported_contexts"], [64, 168, 336])
        self.assertEqual(result["physical_lookback_candles"]["336"], 337)
        self.assertFalse(result["eligible"])

    def test_pairing_requires_identical_ordered_origins(self) -> None:
        good = validate_origin_pairing({"a": ("x", "y"), "b": ("x", "y")})
        bad = validate_origin_pairing({"a": ("x", "y"), "b": ("y", "x")})
        self.assertTrue(good["paired"])
        self.assertFalse(bad["paired"])

    def test_default_report_is_explicitly_blocked_without_skill_claim(self) -> None:
        report = build_report()
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["inference_performed"])
        self.assertFalse(report["forecast_skill_claimed"])
        self.assertTrue(report["blocked_reasons"])


if __name__ == "__main__":
    unittest.main()
