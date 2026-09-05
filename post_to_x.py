#!/usr/bin/env python3
"""Post the generated forecast text to X using a Twikit web session."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from twikit import Client


TWEET_PATH = Path("tweet.txt")
STATUS_PATH = Path("x_post_status.json")
COOKIES_ENV = "X_COOKIES_JSON"


def write_status(status: str, **extra: object) -> None:
    payload = {"status": status, **extra}
    STATUS_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_cookies() -> dict[str, str]:
    raw = os.getenv(COOKIES_ENV)
    if not raw:
        raise RuntimeError(
            f"Required GitHub secret/environment variable {COOKIES_ENV} is not set"
        )

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


async def post() -> None:
    if not TWEET_PATH.exists():
        raise RuntimeError(f"{TWEET_PATH} does not exist; run btc_forecast.py first")

    text = TWEET_PATH.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError(f"{TWEET_PATH} is empty")
    if len(text) > 280:
        raise RuntimeError(f"Refusing to post {len(text)} characters to X")

    client = Client(language="en-US")
    client.set_cookies(load_cookies(), clear_cookies=True)

    try:
        tweet = await client.create_tweet(text=text)
    except Exception as exc:
        write_status(
            "failed",
            provider="twikit",
            error_type=type(exc).__name__,
            error=str(exc),
        )
        print(f"::error::Twikit failed to post to X: {type(exc).__name__}: {exc}")
        raise

    tweet_id = getattr(tweet, "id", None)
    if not tweet_id:
        write_status(
            "failed",
            provider="twikit",
            error="Twikit returned no post ID",
        )
        raise RuntimeError("Twikit returned no post ID")

    write_status("posted", provider="twikit", post_id=str(tweet_id))
    print(f"Posted forecast to X through Twikit. Post ID: {tweet_id}")


def main() -> None:
    try:
        asyncio.run(post())
    except Exception:
        if not STATUS_PATH.exists():
            write_status("failed", provider="twikit", error="Posting setup failed")
        raise


if __name__ == "__main__":
    main()
