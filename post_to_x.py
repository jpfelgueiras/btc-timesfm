#!/usr/bin/env python3
"""Post the generated forecast text to X using OAuth 1.0a user credentials."""

from __future__ import annotations

import json
import os
from pathlib import Path

import tweepy


TWEET_PATH = Path("tweet.txt")
STATUS_PATH = Path("x_post_status.json")
REQUIRED_ENV = (
    "X_API_KEY",
    "X_API_SECRET",
    "X_ACCESS_TOKEN",
    "X_ACCESS_TOKEN_SECRET",
)


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value


def write_status(status: str, **extra: object) -> None:
    payload = {"status": status, **extra}
    STATUS_PATH.write_text(json.dumps(payload, indent=2) + "\n")


def main() -> None:
    if not TWEET_PATH.exists():
        raise RuntimeError(f"{TWEET_PATH} does not exist; run btc_forecast.py first")

    text = TWEET_PATH.read_text().strip()
    if not text:
        raise RuntimeError(f"{TWEET_PATH} is empty")
    if len(text) > 280:
        raise RuntimeError(f"Refusing to post {len(text)} characters to X")

    credentials = {name: require_env(name) for name in REQUIRED_ENV}
    client = tweepy.Client(
        consumer_key=credentials["X_API_KEY"],
        consumer_secret=credentials["X_API_SECRET"],
        access_token=credentials["X_ACCESS_TOKEN"],
        access_token_secret=credentials["X_ACCESS_TOKEN_SECRET"],
    )

    try:
        response = client.create_tweet(text=text)
    except tweepy.HTTPException as exc:
        status_code = getattr(exc.response, "status_code", None)
        response_text = getattr(exc.response, "text", "") or ""

        if status_code == 402 and "credits depleted" in response_text.lower():
            message = (
                "X API credits are depleted. The forecast was generated successfully, "
                "but X refused the post with HTTP 402. Add API credits in the X Developer "
                "Console, then re-run the workflow."
            )
            write_status(
                "not_posted_no_credits",
                http_status=402,
                reason="credits depleted",
            )
            print(f"::warning::{message}")
            print(message)
            return

        write_status(
            "failed",
            http_status=status_code,
            error=str(exc),
        )
        raise

    tweet_id = response.data.get("id") if response.data else None
    if not tweet_id:
        write_status("failed", error="X API returned no post ID")
        raise RuntimeError("X API returned no post ID")

    write_status("posted", post_id=tweet_id)
    print(f"Posted forecast to X successfully. Post ID: {tweet_id}")


if __name__ == "__main__":
    main()
