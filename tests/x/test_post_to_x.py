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

from btc_timesfm.x import post_to_x
from btc_timesfm.x.x_post_registry import XPostRegistry


class FakeClient:
    instance = None
    preflight_error: Exception | None = None
    publish_error: Exception | None = None
    create_calls = 0

    def __init__(self, language: str) -> None:
        self.language = language
        self.cookies = None
        self.text = None
        FakeClient.instance = self

    def set_cookies(self, cookies, clear_cookies=False) -> None:
        self.cookies = cookies
        self.clear_cookies = clear_cookies

    async def get_bookmarks(self, count: int = 1):
        if self.preflight_error is not None:
            raise self.preflight_error
        return []

    async def create_tweet(self, *, text: str):
        FakeClient.create_calls += 1
        self.text = text
        if self.publish_error is not None:
            raise self.publish_error
        return SimpleNamespace(id="123456")


class PostToXTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeClient.instance = None
        FakeClient.preflight_error = None
        FakeClient.publish_error = None
        FakeClient.create_calls = 0

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
        with patch.dict(os.environ, {post_to_x.COOKIES_ENV: json.dumps(value)}, clear=True):
            self.assertEqual(
                post_to_x.load_cookies(),
                {"auth_token": "abc", "ct0": "def", "extra": "123"},
            )

    def _paths(self, tmp: str) -> tuple[Path, Path, Path, Path, Path]:
        root = Path(tmp)
        return (
            root / "tweet.txt",
            root / "forecast.json",
            root / "status.json",
            root / "prepared.json",
            root / "registry.json",
        )

    def _write_context(self, tweet: Path, forecast: Path, text: str = "hello BTC") -> None:
        tweet.write_text(text, encoding="utf-8")
        forecast.write_text(
            json.dumps(
                {
                    "latest_close_at": "2026-09-05T20:00:00+00:00",
                    "experiment_manifest": {
                        "run_id": "exp-1",
                        "configuration_id": "cfg-1",
                    },
                }
            ),
            encoding="utf-8",
        )

    def test_context_refuses_missing_empty_and_oversize_tweet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tweet, forecast, status, prepared, registry = self._paths(tmp)
            forecast.write_text(
                json.dumps({"latest_close_at": "2026-09-05T20:00:00+00:00"}),
                encoding="utf-8",
            )
            with (
                patch.object(post_to_x, "TWEET_PATH", tweet),
                patch.object(post_to_x, "FORECAST_PATH", forecast),
                patch.object(post_to_x, "STATUS_PATH", status),
                patch.object(post_to_x, "PREPARED_PATH", prepared),
                patch.object(post_to_x, "REGISTRY_PATH", registry),
            ):
                with self.assertRaisesRegex(RuntimeError, "does not exist"):
                    post_to_x.load_post_context()
                tweet.write_text("\n", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "is empty"):
                    post_to_x.load_post_context()
                tweet.write_text("x" * 281, encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "Refusing to post 281"):
                    post_to_x.load_post_context()

    def test_expired_session_fails_preflight_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tweet, forecast, status, prepared, registry = self._paths(tmp)
            self._write_context(tweet, forecast)
            FakeClient.preflight_error = RuntimeError("401 Unauthorized: login required")
            cookies = json.dumps({"auth_token": "abc", "ct0": "def"})
            with (
                patch.object(post_to_x, "TWEET_PATH", tweet),
                patch.object(post_to_x, "FORECAST_PATH", forecast),
                patch.object(post_to_x, "STATUS_PATH", status),
                patch.object(post_to_x, "PREPARED_PATH", prepared),
                patch.object(post_to_x, "REGISTRY_PATH", registry),
                patch.object(post_to_x, "Client", FakeClient),
                patch.object(post_to_x, "apply_twikit_compat", lambda: None),
                patch.dict(os.environ, {post_to_x.COOKIES_ENV: cookies}, clear=True),
            ):
                self.assertFalse(asyncio.run(post_to_x.prepare_post()))

            result = json.loads(status.read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "preflight_failed")
            self.assertEqual(result["failure_class"], "authentication")
            self.assertEqual(FakeClient.create_calls, 0)
            self.assertFalse(registry.exists())

    def test_successful_post_is_persisted_and_rerun_is_duplicate_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tweet, forecast, status, prepared, registry = self._paths(tmp)
            self._write_context(tweet, forecast)
            cookies = json.dumps({"auth_token": "abc", "ct0": "def"})
            env = {post_to_x.COOKIES_ENV: cookies, "GITHUB_RUN_ID": "100"}
            with (
                patch.object(post_to_x, "TWEET_PATH", tweet),
                patch.object(post_to_x, "FORECAST_PATH", forecast),
                patch.object(post_to_x, "STATUS_PATH", status),
                patch.object(post_to_x, "PREPARED_PATH", prepared),
                patch.object(post_to_x, "REGISTRY_PATH", registry),
                patch.object(post_to_x, "Client", FakeClient),
                patch.object(post_to_x, "apply_twikit_compat", lambda: None),
                patch.dict(os.environ, env, clear=True),
            ):
                self.assertTrue(asyncio.run(post_to_x.prepare_post()))
                asyncio.run(post_to_x.publish_post())
                self.assertFalse(asyncio.run(post_to_x.prepare_post()))

            result = json.loads(status.read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "duplicate_skipped")
            self.assertEqual(result["post_id"], "123456")
            self.assertEqual(FakeClient.create_calls, 1)
            records = XPostRegistry(registry)
            item = next(iter(records.data["posts"].values()))
            self.assertEqual(item["status"], "posted")
            self.assertEqual(item["experiment_run_id"], "exp-1")
            self.assertEqual(item["github_run_id"], "100")

    def test_ambiguous_response_failure_locks_future_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tweet, forecast, status, prepared, registry = self._paths(tmp)
            self._write_context(tweet, forecast)
            FakeClient.publish_error = KeyError("urls")
            cookies = json.dumps({"auth_token": "abc", "ct0": "def"})
            env = {post_to_x.COOKIES_ENV: cookies, "GITHUB_RUN_ID": "100"}
            with (
                patch.object(post_to_x, "TWEET_PATH", tweet),
                patch.object(post_to_x, "FORECAST_PATH", forecast),
                patch.object(post_to_x, "STATUS_PATH", status),
                patch.object(post_to_x, "PREPARED_PATH", prepared),
                patch.object(post_to_x, "REGISTRY_PATH", registry),
                patch.object(post_to_x, "Client", FakeClient),
                patch.object(post_to_x, "apply_twikit_compat", lambda: None),
                patch.dict(os.environ, env, clear=True),
            ):
                self.assertTrue(asyncio.run(post_to_x.prepare_post()))
                with self.assertRaises(KeyError):
                    asyncio.run(post_to_x.publish_post())
                FakeClient.publish_error = None
                self.assertFalse(asyncio.run(post_to_x.prepare_post()))

            result = json.loads(status.read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "duplicate_locked")
            item = next(iter(XPostRegistry(registry).data["posts"].values()))
            self.assertEqual(item["status"], "ambiguous")
            self.assertEqual(item["failure_class"], "response_parsing")
            self.assertEqual(FakeClient.create_calls, 1)


if __name__ == "__main__":
    unittest.main()
