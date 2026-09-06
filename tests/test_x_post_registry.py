#!/usr/bin/env python3
"""Tests for the durable X publication idempotency registry."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from x_post_registry import XPostRegistry, idempotency_key


class XPostRegistryTests(unittest.TestCase):
    def test_key_is_deterministic_and_content_sensitive(self) -> None:
        first = idempotency_key("2026-09-05T20:00:00+00:00", "BTC forecast")
        self.assertEqual(first, idempotency_key("2026-09-05T20:00:00+00:00", "BTC forecast"))
        self.assertNotEqual(first, idempotency_key("2026-09-05T21:00:00+00:00", "BTC forecast"))
        self.assertNotEqual(first, idempotency_key("2026-09-05T20:00:00+00:00", "BTC forecast!"))

    def test_posted_reservation_is_never_publishable_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registry.json"
            registry = XPostRegistry(path)
            key = idempotency_key("2026-09-05T20:00:00+00:00", "hello")
            first = registry.reserve(
                key=key,
                origin_at="2026-09-05T20:00:00+00:00",
                text="hello",
                experiment_run_id="exp-1",
                configuration_id="cfg-1",
                github_run_id="100",
            )
            self.assertEqual(first["action"], "publish")
            registry.mark_posted(key, "123")

            second = XPostRegistry(path).reserve(
                key=key,
                origin_at="2026-09-05T20:00:00+00:00",
                text="hello",
                experiment_run_id="exp-2",
                configuration_id="cfg-1",
                github_run_id="101",
            )
            self.assertEqual(second["action"], "duplicate")
            self.assertEqual(second["record"]["post_id"], "123")

    def test_ambiguous_attempt_is_locked_but_auth_failure_can_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registry.json"
            registry = XPostRegistry(path)
            key = idempotency_key("2026-09-05T20:00:00+00:00", "hello")
            registry.reserve(
                key=key,
                origin_at="2026-09-05T20:00:00+00:00",
                text="hello",
                experiment_run_id="exp-1",
                configuration_id="cfg-1",
                github_run_id="100",
            )
            registry.mark_failed(
                key,
                failure_class="authentication",
                error_type="Unauthorized",
                error_message="401",
                ambiguous=False,
            )
            retry = registry.reserve(
                key=key,
                origin_at="2026-09-05T20:00:00+00:00",
                text="hello",
                experiment_run_id="exp-1",
                configuration_id="cfg-1",
                github_run_id="101",
            )
            self.assertEqual(retry["action"], "publish")
            self.assertTrue(retry["retry"])
            self.assertEqual(retry["record"]["attempts"], 2)

            registry.mark_failed(
                key,
                failure_class="response_parsing",
                error_type="KeyError",
                error_message="urls",
                ambiguous=True,
            )
            locked = registry.reserve(
                key=key,
                origin_at="2026-09-05T20:00:00+00:00",
                text="hello",
                experiment_run_id="exp-1",
                configuration_id="cfg-1",
                github_run_id="102",
            )
            self.assertEqual(locked["action"], "locked")
            self.assertEqual(locked["record"]["status"], "ambiguous")


if __name__ == "__main__":
    unittest.main()
