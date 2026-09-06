#!/usr/bin/env python3
"""Unit tests for local Twikit compatibility helpers."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import twikit_compat
from twikit_compat import _extract_create_tweet_id, _normalize_user_data


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

    def test_extract_create_tweet_id_from_current_result_shapes(self) -> None:
        direct = {"data": {"create_tweet": {"tweet_results": {"result": {"rest_id": "123456"}}}}}
        nested = {
            "data": {
                "create_tweet": {"tweet_results": {"result": {"tweet": {"rest_id": "654321"}}}}
            }
        }
        legacy = {
            "data": {
                "create_tweet": {"tweet_results": {"result": {"legacy": {"id_str": "111222"}}}}
            }
        }
        self.assertEqual(_extract_create_tweet_id(direct), "123456")
        self.assertEqual(_extract_create_tweet_id(nested), "654321")
        self.assertEqual(_extract_create_tweet_id(legacy), "111222")

    def test_extract_create_tweet_id_does_not_use_account_id(self) -> None:
        payload = {
            "data": {
                "create_tweet": {
                    "tweet_results": {
                        "result": {"core": {"user_results": {"result": {"rest_id": "account-42"}}}}
                    }
                }
            }
        }
        self.assertIsNone(_extract_create_tweet_id(payload))

    def test_client_patch_recovers_id_when_full_tweet_parser_returns_none(self) -> None:
        payload = {
            "data": {"create_tweet": {"tweet_results": {"result": {"rest_id": "987654321"}}}}
        }

        async def fake_create(client, *args, **kwargs):
            setattr(client, twikit_compat._CREATE_TWEET_RESPONSE_ATTR, payload)
            return None

        client = SimpleNamespace()
        with patch.object(twikit_compat, "_ORIGINAL_CLIENT_CREATE_TWEET", fake_create):
            result = asyncio.run(twikit_compat._patched_client_create_tweet(client, text="BTC"))
        self.assertEqual(result.id, "987654321")

    def test_client_patch_recovers_id_when_parser_raises_after_acceptance(self) -> None:
        payload = {
            "data": {
                "create_tweet": {"tweet_results": {"result": {"tweet": {"rest_id": "777888999"}}}}
            }
        }

        async def fake_create(client, *args, **kwargs):
            setattr(client, twikit_compat._CREATE_TWEET_RESPONSE_ATTR, payload)
            raise KeyError("core")

        client = SimpleNamespace()
        with patch.object(twikit_compat, "_ORIGINAL_CLIENT_CREATE_TWEET", fake_create):
            result = asyncio.run(twikit_compat._patched_client_create_tweet(client, text="BTC"))
        self.assertEqual(result.id, "777888999")


if __name__ == "__main__":
    unittest.main()
