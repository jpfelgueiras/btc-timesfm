#!/usr/bin/env python3
"""Durable idempotency registry for forecast posts published to X."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REGISTRY_VERSION = 1
DEFAULT_REGISTRY_PATH = Path(".state/x_post_registry.json")
RETRYABLE_FAILURE_CLASSES = {"authentication", "rate_limit"}
BLOCKING_STATUSES = {"reserved", "ambiguous", "posted"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def idempotency_key(origin_at: str, text: str) -> str:
    payload = f"{origin_at}\n{text}".encode("utf-8")
    return "xpost-" + hashlib.sha256(payload).hexdigest()


class XPostRegistry:
    """Small JSON registry persisted beside the durable forecast-history asset."""

    def __init__(self, path: Path | str = DEFAULT_REGISTRY_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": REGISTRY_VERSION, "posts": {}}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("X post registry must contain a JSON object")
        version = payload.get("version")
        if version != REGISTRY_VERSION:
            raise RuntimeError(
                f"Unsupported X post registry version {version!r}; expected {REGISTRY_VERSION}"
            )
        posts = payload.get("posts")
        if not isinstance(posts, dict):
            raise RuntimeError("X post registry is missing its posts mapping")
        return payload

    def _save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def get(self, key: str) -> dict[str, Any] | None:
        item = self.data["posts"].get(key)
        return dict(item) if isinstance(item, dict) else None

    def reserve(
        self,
        *,
        key: str,
        origin_at: str,
        text: str,
        experiment_run_id: str | None,
        configuration_id: str | None,
        github_run_id: str | None,
        provider: str = "twikit",
    ) -> dict[str, Any]:
        now = _utc_now()
        posts: dict[str, Any] = self.data["posts"]
        existing = posts.get(key)
        if isinstance(existing, dict):
            if existing.get("origin_at") != origin_at or existing.get("content_sha256") != content_sha256(text):
                raise RuntimeError("Idempotency-key collision with different forecast content")
            failure_class = existing.get("failure_class")
            if existing.get("status") == "failed" and failure_class in RETRYABLE_FAILURE_CLASSES:
                existing["status"] = "reserved"
                existing["updated_at"] = now
                existing["github_run_id"] = github_run_id
                existing["attempts"] = int(existing.get("attempts", 1)) + 1
                existing["failure_class"] = None
                existing["error_type"] = None
                existing["error_message"] = None
                self._save()
                return {"action": "publish", "retry": True, "record": dict(existing)}
            action = "duplicate" if existing.get("status") == "posted" else "locked"
            return {"action": action, "retry": False, "record": dict(existing)}

        record: dict[str, Any] = {
            "idempotency_key": key,
            "origin_at": origin_at,
            "content_sha256": content_sha256(text),
            "experiment_run_id": experiment_run_id,
            "configuration_id": configuration_id,
            "github_run_id": github_run_id,
            "provider": provider,
            "status": "reserved",
            "post_id": None,
            "failure_class": None,
            "error_type": None,
            "error_message": None,
            "attempts": 1,
            "created_at": now,
            "updated_at": now,
            "posted_at": None,
        }
        posts[key] = record
        self._save()
        return {"action": "publish", "retry": False, "record": dict(record)}

    def mark_posted(self, key: str, post_id: str) -> dict[str, Any]:
        record = self._require(key)
        now = _utc_now()
        record.update(
            {
                "status": "posted",
                "post_id": str(post_id),
                "failure_class": None,
                "error_type": None,
                "error_message": None,
                "posted_at": now,
                "updated_at": now,
            }
        )
        self._save()
        return dict(record)

    def mark_failed(
        self,
        key: str,
        *,
        failure_class: str,
        error_type: str,
        error_message: str,
        ambiguous: bool,
    ) -> dict[str, Any]:
        record = self._require(key)
        record.update(
            {
                "status": "ambiguous" if ambiguous else "failed",
                "failure_class": failure_class,
                "error_type": error_type,
                "error_message": error_message,
                "updated_at": _utc_now(),
            }
        )
        self._save()
        return dict(record)

    def _require(self, key: str) -> dict[str, Any]:
        record = self.data["posts"].get(key)
        if not isinstance(record, dict):
            raise RuntimeError(f"No X post reservation exists for {key}")
        return record

    def stats(self) -> dict[str, int]:
        counts = {"reserved": 0, "posted": 0, "failed": 0, "ambiguous": 0}
        for item in self.data["posts"].values():
            if isinstance(item, dict) and item.get("status") in counts:
                counts[str(item["status"])] += 1
        return counts
