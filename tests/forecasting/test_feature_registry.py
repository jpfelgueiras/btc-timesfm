from __future__ import annotations

import unittest

from btc_timesfm.forecasting.feature_registry import (
    CROSS_ASSET_FEATURE_NAMES,
    MARKET_FEATURE_NAMES,
    resolve_feature_set,
    validate_feature_set,
)


class FeatureRegistryTests(unittest.TestCase):
    def test_resolution_is_stable_and_tracks_exact_lineage(self) -> None:
        names = [MARKET_FEATURE_NAMES[0], CROSS_ASSET_FEATURE_NAMES[0]]
        first = resolve_feature_set(names)
        second = resolve_feature_set(reversed(names))

        self.assertEqual(first, second)
        self.assertTrue(first["lineage_sha256"])
        self.assertEqual(first["sources"], {"cross_asset_signals": 1, "market_data": 1})

    def test_validation_rejects_tampered_compatibility_contract(self) -> None:
        feature_set = resolve_feature_set([MARKET_FEATURE_NAMES[0]])
        validate_feature_set(feature_set)
        feature_set["registry_version"] = "2"

        with self.assertRaisesRegex(ValueError, "incompatible registry_version"):
            validate_feature_set(feature_set)


if __name__ == "__main__":
    unittest.main()
