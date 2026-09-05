#!/usr/bin/env python3
"""Safely publish a generated forecast to X through a Twikit web session."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from twikit import Client

from twikit_compat import apply_twikit_compat
from x_post_registry import DEFAULT_REGISTRY_PATH, XPostRegistry, idempotency_key


TWEET_PATH = Path("tweet.txt")
FORECAST_PATH = Path("forecast.json")
STATUS_PATH = Path("x_post_status.json")
PREPARED_PATH = Path(".state/x_post_prepared.json")
REGISTRY_PATH = DEFAULT_REGISTRY_PATH
COOKIES_ENV = "X_COOKIES_JSON"


def write_status(status: str, **extra: object) -> None:
    payload = {"status": status, **extra}
    STATUS_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _set_output(name: str, value: str) -> None:
    output = os.getenv("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def load_cookies() -> dict[str, str]:
    raw = os.getenv(COOKIES_ENV)
    if not raw:
        raise RuntimeError(f"Required GitHub secret/environment variable {COOKIES_ENV} is not set")

    try:
        cookies = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{COOKIES_ENV} is not valid JSON") from exc

    if not isinstance(cookies, dict) or not cookies:
        raise RuntimeError(f"{COOKIES_ENV} must contain a non-empty JSON object")

    missing = [name for name in ("auth_token", "ct0") if not cookies.get(name)]
    if missing:
        raise RuntimeError(
            f"{COOKIES_ENV} is missing required X session cookie(s): {', '.join(missing)}"
        )

    return {str(key): str(value) for key, value in cookies.items()}


def load_post_context() -> dict[str, Any]:
    if not TWEET_PATH.exists():
        raise RuntimeError(f"{TWEET_PATH} does not exist; run btc_forecast.py first")
    text = TWEET_PATH.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError(f"{TWEET_PATH} is empty")
    if len(text) > 280:
        raise RuntimeError(f"Refusing to post {len(text)} characters to X")

    if not FORECAST_PATH.exists():
        raise RuntimeError(f"{FORECAST_PATH} does not exist; forecast metadata is required")
    forecast = json.loads(FORECAST_PATH.read_text(encoding="utf-8"))
    if not isinstance(forecast, dict):
        raise RuntimeError(f"{FORECAST_PATH} must contain a JSON object")
    origin_at = forecast.get("latest_close_at")
    if not isinstance(origin_at, str) or not origin_at:
        raise RuntimeError("forecast.json is missing latest_close_at")
    manifest = forecast.get("experiment_manifest")
    if not isinstance(manifest, dict):
        manifest = {}

    return {
        "text": text,
        "origin_at": origin_at,
        "idempotency_key": idempotency_key(origin_at, text),
        "experiment_run_id": manifest.get("run_id"),
        "configuration_id": manifest.get("configuration_id"),
        "github_run_id": os.getenv("GITHUB_RUN_ID"),
    }


def classify_twikit_error(exc: BaseException) -> str:
    type_name = type(exc).__name__.lower()
    message = str(exc).lower()
    combined = f"{type_name} {message}"
    if any(token in combined for token in ("401", "403", "unauthorized", "forbidden", "auth", "login", "cookie", "challenge")):
        return "authentication"
    if any(token in combined for token in ("429", "rate limit", "ratelimit", "too many requests")):
        return "rate_limit"
    if any(token in combined for token in ("json", "parse", "keyerror", "couldn't get", "could not get", "unexpected response")):
        return "response_parsing"
    if any(token in combined for token in ("timeout", "connection", "network", "transport", "dns")):
        return "network"
    return "provider_error"


def _client() -> Client:
    apply_twikit_compat()
    client = Client(language="en-US")
    client.set_cookies(load_cookies(), clear_cookies=True)
    return client


async def session_preflight(client: Client) -> None:
    """Touch an authenticated, read-only endpoint before any publication attempt."""
    await client.get_bookmarks(count=1)


async def prepare_post() -> bool:
    """Validate the X session and persist a durable reservation before posting."""
    try:
        context = load_post_context()
        client = _client()
        await session_preflight(client)
    except Exception as exc:
        failure_class = classify_twikit_error(exc)
        write_status(
            "preflight_failed",
            provider="twikit",
            failure_class=failure_class,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        _set_output("publish", "false")
        _set_output("reason", failure_class)
        print(f"::warning::X session preflight failed ({failure_class}): {type(exc).__name__}: {exc}")
        return False

    registry = XPostRegistry(REGISTRY_PATH)
    reservation = registry.reserve(
        key=context["idempotency_key"],
        origin_at=context["origin_at"],
        text=context["text"],
        experiment_run_id=context["experiment_run_id"],
        configuration_id=context["configuration_id"],
        github_run_id=context["github_run_id"],
    )
    record = reservation["record"]
    action = str(reservation["action"])
    if action != "publish":
        status = "duplicate_skipped" if action == "duplicate" else "duplicate_locked"
        write_status(
            status,
            provider="twikit",
            idempotency_key=context["idempotency_key"],
            origin_at=context["origin_at"],
            existing_status=record.get("status"),
            post_id=record.get("post_id"),
        )
        _set_output("publish", "false")
        _set_output("reason", status)
        print(f"Skipping X publication: idempotency registry status is {record.get('status')!r}.")
        return False

    PREPARED_PATH.parent.mkdir(parents=True, exist_ok=True)
    PREPARED_PATH.write_text(
        json.dumps(
            {
                "idempotency_key": context["idempotency_key"],
                "origin_at": context["origin_at"],
                "github_run_id": context["github_run_id"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    write_status(
        "prepared",
        provider="twikit",
        idempotency_key=context["idempotency_key"],
        origin_at=context["origin_at"],
        retry=bool(reservation.get("retry")),
    )
    _set_output("publish", "true")
    _set_output("reason", "prepared")
    return True


async def publish_post() -> None:
    """Publish only a reservation created by this workflow run."""
    context = load_post_context()
    if not PREPARED_PATH.exists():
        raise RuntimeError("X post was not prepared; refusing an unreserved publication attempt")
    prepared = json.loads(PREPARED_PATH.read_text(encoding="utf-8"))
    if not isinstance(prepared, dict) or prepared.get("idempotency_key") != context["idempotency_key"]:
        raise RuntimeError("Prepared X post does not match the current forecast/content")
    prepared_run = prepared.get("github_run_id")
    if prepared_run and context["github_run_id"] and prepared_run != context["github_run_id"]:
        raise RuntimeError("Prepared X post belongs to a different GitHub Actions run")

    registry = XPostRegistry(REGISTRY_PATH)
    existing = registry.get(context["idempotency_key"])
    if existing is None or existing.get("status") != "reserved":
        raise RuntimeError("X post reservation is no longer publishable")

    client = _client()
    try:
        tweet = await client.create_tweet(text=context["text"])
    except Exception as exc:
        failure_class = classify_twikit_error(exc)
        ambiguous = failure_class not in {"authentication", "rate_limit"}
        registry.mark_failed(
            context["idempotency_key"],
            failure_class=failure_class,
            error_type=type(exc).__name__,
            error_message=str(exc),
            ambiguous=ambiguous,
        )
        write_status(
            "failed",
            provider="twikit",
            idempotency_key=context["idempotency_key"],
            failure_class=failure_class,
            ambiguous=ambiguous,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        print(f"::error::Twikit failed to post to X ({failure_class}): {type(exc).__name__}: {exc}")
        raise

    tweet_id = getattr(tweet, "id", None)
    if not tweet_id:
        registry.mark_failed(
            context["idempotency_key"],
            failure_class="response_parsing",
            error_type="MissingPostId",
            error_message="Twikit returned no post ID",
            ambiguous=True,
        )
        write_status(
            "failed",
            provider="twikit",
            idempotency_key=context["idempotency_key"],
            failure_class="response_parsing",
            ambiguous=True,
            error="Twikit returned no post ID",
        )
        raise RuntimeError("Twikit returned no post ID")

    record = registry.mark_posted(context["idempotency_key"], str(tweet_id))
    write_status(
        "posted",
        provider="twikit",
        idempotency_key=context["idempotency_key"],
        origin_at=context["origin_at"],
        post_id=str(tweet_id),
        experiment_run_id=record.get("experiment_run_id"),
        github_run_id=record.get("github_run_id"),
    )
    print(f"Posted forecast to X through Twikit. Post ID: {tweet_id}")


async def post() -> None:
    """Compatibility helper for local/manual callers: prepare then publish."""
    if await prepare_post():
        await publish_post()


def main() -> None:
    parser = argparse.ArgumentParser(description="Safely publish the generated forecast to X")
    parser.add_argument("command", nargs="?", choices=("prepare", "publish"), default="publish")
    args = parser.parse_args()

    if args.command == "prepare":
        asyncio.run(prepare_post())
        return

    try:
        asyncio.run(publish_post())
    except Exception:
        if not STATUS_PATH.exists():
            write_status("failed", provider="twikit", error="Posting setup failed")
        raise


if __name__ == "__main__":
    main()
