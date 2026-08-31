# Phase 7C — Scheduled Publishing / Execution Design

## Goal

Execute due, tenant-owned scheduled image content safely and exactly once from the
application's perspective, using the existing encrypted OAuth connection path.

## Scope

Phase 7C adds a database-backed execution service and Flask CLI command. It
publishes `image` content only. `video`, `carousel`, and `reel` items are
explicitly terminally failed before any provider request with the safe code
`unsupported_content_type`.

No UI, automatic retries, worker framework, deployment, historical migration,
or legacy/global-token publishing behavior is changed.

## Lifecycle and Claiming

Content items use `scheduled`, `publishing`, `published`, and `failed` states.
Only a due `scheduled` row is eligible. A short database transaction locks each
candidate with `FOR UPDATE SKIP LOCKED`, confirms the state and scheduled time,
then transitions it to `publishing`. The claim is committed before any network
operation. This makes repeated scheduler runs and concurrent workers unable to
claim the same item.

`published` and `failed` are terminal in Phase 7C. A process crash after the
claim leaves `publishing` for later reconciliation rather than replaying an
unknown provider operation. Phase 7C deliberately has no automatic retry or
lease reaper for publishing work.

## Provider Integration

The service receives an item ID and its tenant-bound Instagram account ID only
from the claim. It resolves credentials through
`resolve_oauth_instagram_account_runtime`, which verifies the account identity,
connection state, token state, and encrypted credential. It then performs the
Instagram Graph image container and `media_publish` flow using that resolved
account ID and token. It does not call the legacy `webhook.py` global-token
publisher.

The provider call is made outside the claim transaction. Completion is recorded
in a fresh transaction conditioned on the item still being `publishing`.

## Failure Semantics

Deterministic failures before a provider request—unsupported type, unusable
connection, and credential resolution failure—are marked `failed` with a short
safe error code and `provider_request_may_have_been_sent=False`.

Provider rejections with a confirmed response are also failed with a sanitized
code. Network failures, timeouts, malformed provider responses, and failures
after the publish request begins are marked `failed` with
`provider_request_may_have_been_sent=True`. These rows are never automatically
resent. The persisted flag and safe error code give a future reconciliation
phase enough information to decide whether to query Instagram before any
operator-approved retry.

Tokens, authorization headers, request URLs containing credentials, and raw
provider payloads are neither logged nor persisted. Structured logs carry only
event name, content ID, Instagram account ID, lifecycle state, and safe error
code.

## Schema

A new forward-only Alembic revision adds:

- `published_at` (UTC timestamp)
- `provider_media_id` (provider publication identifier)
- `publish_error_code` (safe error code)
- `provider_request_may_have_been_sent` (boolean, default false)
- an index supporting the due scheduled query

Existing `status` and `scheduled_for` fields are reused. Historical migrations
are not changed.

## Entry Point and Tests

`flask publish-scheduled-content` resolves `DATABASE_URL`, creates a session
factory, invokes the service with a bounded batch size, and reports a
token-free summary suitable for Railway Cron.

Focused tests use SQLite for lifecycle and tenant behavior and a narrow provider
transport double for the external Graph boundary. They cover due selection,
future and terminal exclusion, concurrent/repeated claiming, successful image
publication, pre-provider failures, ambiguous provider failures, tenant
isolation, and no duplicate provider publication.
