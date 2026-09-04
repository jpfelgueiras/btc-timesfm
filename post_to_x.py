#!/usr/bin/env python3
"""Post the generated forecast text to X using OAuth 1.0a user credentials."""

from __future__ import annotations

import os
from pathlib import Path

import tweepy


TWEET_PATH = Path("tweet.txt")
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

    response = client.create_tweet(text=text)
    tweet_id = response.data.get("id") if response.data else None
    if not tweet_id:
        raise RuntimeError("X API returned no post ID")

    print(f"Posted forecast to X successfully. Post ID: {tweet_id}")


if __name__ == "__main__":
    main()
