"""Compatibility patches for Twikit 2.3.3 against X's current frontend.

X changed several response/frontend details in 2026 that break released
Twikit 2.3.3. Keep the workarounds here so the project can stay pinned to the
official release while upstream fixes are pending.

Remove individual patches as soon as a Twikit release includes them.
"""

from __future__ import annotations

import copy
import re

from twikit.guest.user import User as GuestUser
from twikit.user import User as AuthenticatedUser
from twikit.x_client_transaction.transaction import ClientTransaction


ON_DEMAND_FILE_REGEX = re.compile(r',([0-9]+):["\']ondemand\.s["\']')
ON_DEMAND_HASH_PATTERN = r',{}:["\']([0-9a-f]+)["\']'
INDICES_REGEX = re.compile(r"\[([0-9]+)\],\s*16")

_ORIGINAL_AUTH_USER_INIT = AuthenticatedUser.__init__
_ORIGINAL_GUEST_USER_INIT = GuestUser.__init__


async def _patched_get_indices(self, home_page_response, session, headers):
    """Resolve X's current ondemand.s webpack chunk and KEY_BYTE indices."""
    key_byte_indices: list[str] = []
    response = self.validate_response(home_page_response) or self.home_page_response
    response_text = str(response)

    on_demand_match = ON_DEMAND_FILE_REGEX.search(response_text)
    if on_demand_match:
        chunk_index = on_demand_match.group(1)
        hash_match = re.search(
            ON_DEMAND_HASH_PATTERN.format(chunk_index),
            response_text,
        )
        if hash_match:
            file_hash = hash_match.group(1)
            on_demand_file_url = (
                f"https://abs.twimg.com/responsive-web/client-web/ondemand.s.{file_hash}a.js"
            )
            on_demand_file_response = await session.request(
                method="GET",
                url=on_demand_file_url,
                headers=headers,
            )
            key_byte_indices.extend(
                match.group(1) for match in INDICES_REGEX.finditer(on_demand_file_response.text)
            )

    if not key_byte_indices:
        raise RuntimeError(
            "Couldn't get KEY_BYTE indices even with the Twikit compatibility patch. "
            "X likely changed its frontend format again."
        )

    key_byte_indices_int = list(map(int, key_byte_indices))
    return key_byte_indices_int[0], key_byte_indices_int[1:]


def _normalize_user_data(data: dict) -> dict:
    """Add optional legacy fields that Twikit 2.3.3 assumes always exist."""
    normalized = copy.deepcopy(data)
    legacy = normalized.get("legacy")
    if not isinstance(legacy, dict):
        return normalized

    entities = legacy.setdefault("entities", {})
    if isinstance(entities, dict):
        description = entities.setdefault("description", {})
        if isinstance(description, dict):
            description.setdefault("urls", [])

    legacy.setdefault("withheld_in_countries", [])
    legacy.setdefault("pinned_tweet_ids_str", [])
    return normalized


def _patched_authenticated_user_init(self, client, data):
    _ORIGINAL_AUTH_USER_INIT(self, client, _normalize_user_data(data))


def _patched_guest_user_init(self, client, data):
    _ORIGINAL_GUEST_USER_INIT(self, client, _normalize_user_data(data))


def apply_twikit_compat() -> None:
    """Apply compatibility patches once for the current Python process."""
    if getattr(ClientTransaction, "_btc_timesfm_compat_applied", False):
        return

    ClientTransaction.get_indices = _patched_get_indices
    AuthenticatedUser.__init__ = _patched_authenticated_user_init
    GuestUser.__init__ = _patched_guest_user_init
    ClientTransaction._btc_timesfm_compat_applied = True
