# Response-triggered Instagram Follow-ups Implementation Plan

> **For agentic workers:** Execute inline with focused test checkpoints. The subsequent final-review approval authorizes commit/push and staging code/migration after all affected gates pass.

**Goal:** Schedule a general direct-message follow-up only after a qualifying recipient reply, inside Meta's 24-hour reply window.

**Architecture:** Persist a one-time, account-scoped interaction after the initial comment private reply. A valid inbound reply consumes that interaction and creates the existing delayed job with a nullable expiry. Prompt-request jobs retain their current paths and null expiry.

**Tech Stack:** Python, SQLAlchemy, Alembic, unittest, Flask webhook/scheduler.

**Spec:** `docs/superpowers/specs/2026-09-04-response-triggered-instagram-followups-design.md`

## Global Constraints

- No production, Railway-variable, staging-configuration, or live Instagram interaction changes.
- Preserve tenant/account isolation, OAuth credential semantics, webhook idempotency, dedupe, lease lifecycle, publishing, and Cron.
- Use fixed safe log labels only.

---

### Task 1: Durable interaction and deadline state

**Files:**
- Modify: `database/models.py`
- Modify: `database/oauth_followups.py`
- Create: `alembic/versions/20260904_02_response_triggered_oauth_followups.py`
- Test: `tests/test_oauth_followups.py`

- [x] Add an `InstagramOAuthDirectMessageInteraction` model, unique per account/source comment, with recipient, initial-reply timestamp, optional consuming reply ID/timestamp, and account/recipient lookup index.
- [x] Add nullable `provider_window_expires_at` to `InstagramOAuthFollowupJob` and `ClaimedOAuthFollowup`.
- [x] Add helpers to save a qualifying interaction and atomically consume one for an account/recipient/inbound message.
- [x] Extend scheduling with an optional expiry, reject a run time at or after it, and retain null for legacy callers.
- [x] Add `outside_provider_window` as a terminal job result and test claim/finalization behavior.
- [x] Add an Alembic migration from `20260904_01`; its downgrade blocks only when response-triggered state exists and otherwise drops the new table/column.

### Task 2: Webhook lifecycle

**Files:**
- Modify: `webhook.py`
- Test: `tests/test_webhook_tenant_routing.py`

- [x] Change the direct comment branch to persist interaction after a successful initial private reply, never schedule a follow-up, and log `waiting_for_recipient_reply`.
- [x] Add a direct-message inbound branch after event claim and runtime-config resolution. It consumes only an interaction owned by the routed account, validates enabled follow-up configuration and the 24-hour deadline, then schedules the existing job using the inbound message as its source.
- [x] Keep echoes and malformed/non-message events out of the branch; retain legacy prompt quick-reply and arbitrary-message behavior unchanged for `prompt_request`.
- [x] Add fixed safe logs for eligible, scheduled, disabled, no interaction, cooldown, and window-expiry outcomes; duplicate event claiming remains unchanged.
- [x] Preserve provider timestamps and minimal non-reply/attachment flags through durable ingress; reject invalid/future timestamps and pre-interaction replies. Recheck expiry at Graph dispatch after credential resolution.

### Task 3: Regression and verification

**Files:**
- Modify: `tests/test_webhook_tenant_routing.py`
- Modify: `tests/test_oauth_followups.py`
- Modify: `tests/test_oauth_engagement_gate.py`

- [x] Cover no job before reply; persistence only after initial-DM success; eligible reply; duplicate; unrelated, echo, receipt; disabled/empty config; cooldown; account isolation; deadline worker block; and prompt regression.
- [x] Run focused webhook, engagement, follow-up, migration, publishing/Cron tests; compile Python; run `git diff --check`.
- [x] Compare scheduler/analytics assertions with an isolated HEAD archive: the same five failures occur in both trees.
