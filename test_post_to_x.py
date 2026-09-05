#!/usr/bin/env python3
"""Unit tests for X session validation and posting safeguards."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import post_to_x


class FakeClient:
    instance = None

    def __init__(self, language: str) -> None:
        self.language = language
        self.cookies = None
        self.text = None
        FakeClient.instance = self

    def set_cookies(self, cookies, clear_cookies=False) -> None:
        self.cookies = cookies
        self.clear_cookies = clear_cookies

    async def create_tweet(self, *, text: str):
        self.text = text
        return SimpleNamespace(id="123456")


class PostToXTests(unittest.TestCase):
    def test_load_cookies_requires_environment_variable(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "is not set"):
                post_to_x.load_cookies()

    def test_load_cookies_rejects_invalid_or_incomplete_json(self) -> None:
        with patch.dict(os.environ, {post_to_x.COOKIES_ENV: "not-json"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "not valid JSON"):
                post_to_x.load_cookies()

        with patch.dict(
            os.environ,
            {post_to_x.COOKIES_ENV: json.dumps({"auth_token": "abc"})},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "ct0"):
                post_to_x.load_cookies()

    def test_load_cookies_accepts_required_session_values(self) -> None:
        value = {"auth_token": "abc", "ct0": "def", "extra": 123}
        with patch.dict(
            os.environ, {post_to_x.COOKIES_ENV: json.dumps(value)}, clear=True
        ):
            self.assertEqual(
                post_to_x.load_cookies(),
                {"auth_token": "abc", "ct0": "def", "extra": "123"},
            )

    def test_post_refuses_missing_empty_and_oversize_tweet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tweet.txt"
            with patch.object(post_to_x, "TWEET_PATH", path):
                with self.assertRaisesRegex(RuntimeError, "does not exist"):
                    asyncio.run(post_to_x.post())

                path.write_text("\n", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "is empty"):
                    asyncio.run(post_to_x.post())

                path.write_text("x" * 281, encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "Refusing to post 281"):
                    asyncio.run(post_to_x.post())

    def test_successful_post_writes_status_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tweet_path = Path(tmp) / "tweet.txt"
            status_path = Path(tmp) / "status.json"
            tweet_path.write_text("hello BTC", encoding="utf-8")
            cookies = json.dumps({"auth_token": "abc", "ct0": "def"})

            with patch.object(post_to_x, "TWEET_PATH", tweet_path), patch.object(
                post_to_x, "STATUS_PATH", status_path
            ), patch.object(post_to_x, "Client", FakeClient), patch.object(
                post_to_x, "apply_twikit_compat", lambda: None
            ), patch.dict(os.environ, {post_to_x.COOKIES_ENV: cookies}, clear=True):
                asyncio.run(post_to_x.post())

            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "posted")
            self.assertEqual(status["post_id"], "123456")
            self.assertEqual(FakeClient.instance.text, "hello BTC")
            self.assertTrue(FakeClient.instance.clear_cookies)


if __name__ == "__main__":
    unittest.main()
