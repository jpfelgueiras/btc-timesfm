# X publication safety

Production X publishing uses Twikit with a browser-session cookie secret, but posting is now guarded by a durable idempotency registry and an authenticated session preflight.

## Idempotency key

Each publication gets a deterministic key derived from:

- the forecast `latest_close_at` origin timestamp; and
- the exact final text in `tweet.txt`.

The SHA-256 key is stable across retries. Re-running the same forecast with the same text therefore resolves to the same registry record.

The registry is stored as `.state/x_post_registry.json` and uploaded to the same machine-managed GitHub Release used for forecast history. Records include the forecast origin, content digest, experiment/configuration IDs, GitHub Actions run ID, attempt count, provider, status and final X post ID when available.

## Two-phase publication

Scheduled or explicitly requested manual publication follows this order:

1. load and validate `X_COOKIES_JSON`;
2. call an authenticated, read-only Twikit endpoint as a session preflight;
3. reserve the deterministic idempotency key;
4. persist the reservation to the durable Release before any write to X;
5. call `create_tweet` only when the current run created a publishable reservation;
6. record the returned post ID and persist the updated registry again.

This intentionally prefers a missed post over a duplicate. If a previous run left a reservation in an uncertain state, later runs do not blindly retry it.

## Failure classes

Publishing distinguishes these classes:

- `authentication` — expired/invalid cookies, login/challenge failures or 401/403-style responses;
- `rate_limit` — 429/rate-limit responses;
- `response_parsing` — Twikit/X response-shape failures, including missing post IDs;
- `network` — transport, DNS, connection and timeout failures;
- `provider_error` — other Twikit/X failures.

Authentication and rate-limit failures from the actual create call are considered safe to retry because X rejected the request. Response-parsing, network and unknown provider failures are treated as ambiguous because the request may have reached X; their registry entry is locked against automatic replay.

A session-preflight failure happens before a reservation is created, so fixing the session secret allows a later run to try normally.

## Statuses

`x_post_status.json` reports `prepared`, `posted`, `duplicate_skipped`, `duplicate_locked`, `preflight_failed` or `failed`. It is included in the Actions summary/artifact.

The durable registry uses `reserved`, `posted`, `failed` and `ambiguous` states. A `posted` record contains the X `post_id` plus experiment and workflow-run metadata.

## Operational recovery

If a registry entry is `ambiguous`, confirm manually whether the post exists on X before changing the registry. Automatic retry is deliberately disabled for this state.

If the session preflight reports `authentication`, refresh the `X_COOKIES_JSON` secret from an authenticated browser session and rerun. Do not paste those cookie values into issues, logs or chat output.
