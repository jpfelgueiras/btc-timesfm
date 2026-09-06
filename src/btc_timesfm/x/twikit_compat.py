"""Compatibility patches for Twikit 2.3.3 against X's current frontend.

X changed several response/frontend details in 2026 that break released
Twikit 2.3.3. Keep the workarounds here so the project can stay pinned to the
official release while upstream fixes are pending.

Remove individual patches as soon as a Twikit release includes them.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from twikit import Client as AuthenticatedClient
from twikit.client.gql import GQLClient
from twikit.guest.user import User as GuestUser
from twikit.user import User as AuthenticatedUser
from twikit.x_client_transaction.transaction import ClientTransaction


ON_DEMAND_FILE_REGEX = re.compile(r',([0-9]+):["\']ondemand\.s["\']')
ON_DEMAND_HASH_PATTERN = r',{}:["\']([0-9a-f]+)["\']'
INDICES_REGEX = re.compile(r"\[([0-9]+)\],\s*16")
_CREATE_TWEET_RESPONSE_ATTR = "_btc_timesfm_last_create_tweet_response"

_ORIGINAL_AUTH_USER_INIT = AuthenticatedUser.__init__
_ORIGINAL_GUEST_USER_INIT = GuestUser.__init__
_ORIGINAL_GQL_CREATE_TWEET = GQLClient.create_tweet
_ORIGINAL_CLIENT_CREATE_TWEET = AuthenticatedClient.create_tweet


@dataclass(frozen=True)
class _CreatedTweetAck:
    """Minimal acknowledgement used when Twikit cannot build a full Tweet object."""

    id: str


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


def _string_id(value: object) -> str | None:
    if isinstance(value, (str, int)):
        text = str(value).strip()
        if text:
            return text
    return None


def _extract_create_tweet_id(payload: Any) -> str | None:
    """Extract the created tweet ID without depending on Twikit's full Tweet parser.

    X can accept CreateTweet while omitting fields that Twikit 2.3.3 requires to
    construct a Tweet object.  The acknowledgement still contains the tweet's
    ``rest_id`` (or legacy ``id_str``), which is sufficient to prove publication.
    Only the CreateTweet result subtree is inspected so a user/account ID cannot
    be mistaken for the newly-created tweet ID.
    """
    if not isinstance(payload, dict):
        return None

    node: Any = payload
    data = node.get("data")
    if isinstance(data, dict):
        create_tweet = data.get("create_tweet")
        if isinstance(create_tweet, dict):
            node = create_tweet.get("tweet_results")

    if not isinstance(node, dict):
        return None

    # X has used both result.rest_id and result.tweet.rest_id shapes.
    candidates: list[dict[str, Any]] = [node]
    result = node.get("result")
    if isinstance(result, dict):
        candidates.append(result)
        tweet = result.get("tweet")
        if isinstance(tweet, dict):
            candidates.append(tweet)
    tweet = node.get("tweet")
    if isinstance(tweet, dict):
        candidates.append(tweet)

    for candidate in candidates:
        tweet_id = _string_id(candidate.get("rest_id"))
        if tweet_id:
            return tweet_id
        legacy = candidate.get("legacy")
        if isinstance(legacy, dict):
            tweet_id = _string_id(legacy.get("id_str"))
            if tweet_id:
                return tweet_id

    return None


async def _patched_gql_create_tweet(self, *args, **kwargs):
    """Capture the raw CreateTweet response before Twikit parses it."""
    result = await _ORIGINAL_GQL_CREATE_TWEET(self, *args, **kwargs)
    response = result[0] if isinstance(result, tuple) and result else None
    base = getattr(self, "base", None)
    if base is not None:
        setattr(base, _CREATE_TWEET_RESPONSE_ATTR, response)
    return result


async def _patched_client_create_tweet(self, *args, **kwargs):
    """Recover a publication acknowledgement when Twikit's Tweet parser drifts."""
    setattr(self, _CREATE_TWEET_RESPONSE_ATTR, None)
    try:
        tweet = await _ORIGINAL_CLIENT_CREATE_TWEET(self, *args, **kwargs)
    except Exception:
        tweet_id = _extract_create_tweet_id(getattr(self, _CREATE_TWEET_RESPONSE_ATTR, None))
        if tweet_id:
            return _CreatedTweetAck(tweet_id)
        raise

    try:
        tweet_id = _string_id(getattr(tweet, "id", None)) if tweet is not None else None
    except (AttributeError, KeyError, TypeError):
        tweet_id = None
    if tweet_id:
        return tweet

    tweet_id = _extract_create_tweet_id(getattr(self, _CREATE_TWEET_RESPONSE_ATTR, None))
    if tweet_id:
        return _CreatedTweetAck(tweet_id)
    return tweet


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
    GQLClient.create_tweet = _patched_gql_create_tweet
    AuthenticatedClient.create_tweet = _patched_client_create_tweet
    ClientTransaction._btc_timesfm_compat_applied = True
