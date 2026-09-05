#!/usr/bin/env python3
"""Create a reusable Twikit X session cookie file interactively.

Run this locally once, then store the resulting x_cookies.json as the
X_COOKIES_JSON GitHub Actions secret. Do not commit the cookie file.
"""

from __future__ import annotations

import asyncio
import getpass
import json
from pathlib import Path

from twikit import Client

from twikit_compat import apply_twikit_compat


OUTPUT_PATH = Path("x_cookies.json")


async def create_session() -> None:
    print("Create a reusable X session for Twikit.")
    print("Credentials are used only for this local login and are not written to disk.\n")

    username = input("X username (without @): ").strip()
    email = input("X email (recommended): ").strip()
    password = getpass.getpass("X password: ")

    if not username or not password:
        raise RuntimeError("Username and password are required")

    apply_twikit_compat()
    client = Client(language="en-US")

    # If X requests a 2FA code, Twikit will prompt for it interactively.
    await client.login(
        auth_info_1=username,
        auth_info_2=email or None,
        password=password,
    )

    cookies = client.get_cookies()
    if not cookies.get("auth_token") or not cookies.get("ct0"):
        raise RuntimeError(
            "Login completed but the expected auth_token/ct0 cookies were not returned"
        )

    OUTPUT_PATH.write_text(
        json.dumps(cookies, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    print(f"\nSaved reusable X session to {OUTPUT_PATH}.")
    print("Store it in GitHub Actions with:")
    print(
        "  gh secret set X_COOKIES_JSON --repo jpfelgueiras/btc-timesfm "
        f"< {OUTPUT_PATH}"
    )
    print(f"Then delete {OUTPUT_PATH} from your machine when you no longer need it.")


def main() -> None:
    asyncio.run(create_session())


if __name__ == "__main__":
    main()
