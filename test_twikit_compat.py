#!/usr/bin/env python3
"""Unit tests for local Twikit compatibility helpers."""

from __future__ import annotations

import unittest

from twikit_compat import _normalize_user_data


class TwikitCompatTests(unittest.TestCase):
    def test_normalize_user_data_adds_optional_legacy_fields(self) -> None:
        original = {
            "legacy": {
                "entities": {"description": {}},
                "name": "Example",
            }
        }
        normalized = _normalize_user_data(original)

        self.assertEqual(normalized["legacy"]["entities"]["description"]["urls"], [])
        self.assertEqual(normalized["legacy"]["withheld_in_countries"], [])
        self.assertEqual(normalized["legacy"]["pinned_tweet_ids_str"], [])
        self.assertNotIn("urls", original["legacy"]["entities"]["description"])

    def test_normalize_user_data_preserves_existing_values(self) -> None:
        original = {
            "legacy": {
                "entities": {"description": {"urls": [{"url": "https://example.com"}]}},
                "withheld_in_countries": ["PT"],
                "pinned_tweet_ids_str": ["123"],
            }
        }
        normalized = _normalize_user_data(original)
        self.assertEqual(
            normalized["legacy"]["entities"]["description"]["urls"],
            [{"url": "https://example.com"}],
        )
        self.assertEqual(normalized["legacy"]["withheld_in_countries"], ["PT"])
        self.assertEqual(normalized["legacy"]["pinned_tweet_ids_str"], ["123"])

    def test_normalize_user_data_without_legacy_is_safe(self) -> None:
        original = {"rest_id": "42"}
        self.assertEqual(_normalize_user_data(original), original)


if __name__ == "__main__":
    unittest.main()
