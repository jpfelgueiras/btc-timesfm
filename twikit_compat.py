"""Compatibility patches for Twikit 2.3.3 against X's current frontend.

X changed the webpack chunk format used to locate ondemand.s.*.js in 2026,
which makes released Twikit raise `Couldn't get KEY_BYTE indices` before any
request can be made. This monkey-patch mirrors the focused transaction parsing
fix proposed upstream while keeping the dependency pinned to the official
Twikit release.

Remove this module once a Twikit release includes the upstream fix.
"""

from __future__ import annotations

import re

from twikit.x_client_transaction.transaction import ClientTransaction


ON_DEMAND_FILE_REGEX = re.compile(r',([0-9]+):["\']ondemand\.s["\']')
ON_DEMAND_HASH_PATTERN = r',{}:["\']([0-9a-f]+)["\']'
INDICES_REGEX = re.compile(r'\[([0-9]+)\],\s*16')


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
                "https://abs.twimg.com/responsive-web/client-web/"
                f"ondemand.s.{file_hash}a.js"
            )
            on_demand_file_response = await session.request(
                method="GET",
                url=on_demand_file_url,
                headers=headers,
            )
            key_byte_indices.extend(
                match.group(1)
                for match in INDICES_REGEX.finditer(on_demand_file_response.text)
            )

    if not key_byte_indices:
        raise RuntimeError(
            "Couldn't get KEY_BYTE indices even with the Twikit compatibility patch. "
            "X likely changed its frontend format again."
        )

    key_byte_indices_int = list(map(int, key_byte_indices))
    return key_byte_indices_int[0], key_byte_indices_int[1:]


def apply_twikit_compat() -> None:
    """Apply compatibility patches once for the current Python process."""
    if getattr(ClientTransaction, "_btc_timesfm_compat_applied", False):
        return

    ClientTransaction.get_indices = _patched_get_indices
    ClientTransaction._btc_timesfm_compat_applied = True
