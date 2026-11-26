# Phase 7C Scheduled Publishing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute due tenant-owned scheduled image content through Instagram with durable, idempotent lifecycle records.

**Architecture:** A new database service atomically claims due rows as `publishing`, releases the transaction before the external Graph request, then completes the row in a fresh transaction. It uses the existing OAuth runtime resolver and leaves ambiguous outcomes terminal rather than retrying them.

**Tech Stack:** Python, SQLAlchemy 2, Alembic, Flask CLI, requests, unittest.

**Spec:** `docs/superpowers/specs/2026-08-31-phase7c-scheduled-publishing-design.md`

## Global Constraints

- Do not modify historical migrations.
- Publish `image` only; `video`, `carousel`, and `reel` fail before a provider request.
- Do not use the legacy/global-token publisher in `webhook.py`.
- No automatic retry, lease reaping, deployment, or manual migration application.
- Never persist or log credentials, tokens, headers, or raw provider payloads.
- Do not commit or push this work.

---

### Task 1: Add publication lifecycle persistence

**Files:**
- Modify: `database/models.py`
- Create: `alembic/versions/20260831_04_phase7c_scheduled_publishing.py`
- Test: `tests/test_phase7c_scheduled_publishing_migration.py`

**Interfaces:**
- Produces: `InstagramContentItem.published_at`, `provider_media_id`, `publish_error_code`, and `provider_request_may_have_been_sent`.

- [ ] **Step 1: Write the failing migration/schema test**

```python
def test_phase7c_revision_is_linear_and_adds_publication_columns():
    module = _load_revision()
    assert module.down_revision == "20260831_03"
    assert set(_added_column_names(module)) == {
        "published_at", "provider_media_id", "publish_error_code",
        "provider_request_may_have_been_sent",
    }
```

- [ ] **Step 2: Run the migration test and verify it fails because the revision is absent.**

- [ ] **Step 3: Add the model fields and the minimal forward-only revision.**

- [ ] **Step 4: Run the migration test and verify it passes.**

### Task 2: Implement claim and completion lifecycle operations

**Files:**
- Create: `database/content_publishing.py`
- Test: `tests/test_phase7c_scheduled_publishing.py`

**Interfaces:**
- Produces: `claim_due_scheduled_content(session, now, limit)`, `mark_content_published(...)`, and `mark_content_publish_failed(...)`.

- [ ] **Step 1: Write failing tests for future exclusion, due claim, terminal exclusion, and a second claim returning no row.**

```python
claimed = claim_due_scheduled_content(session, now=due_time, limit=10)
assert [item.id for item in claimed] == [due_item.id]
assert due_item.status == "publishing"
assert claim_due_scheduled_content(session, now=due_time, limit=10) == []
```

- [ ] **Step 2: Run the focused tests and verify the expected import failure.**

- [ ] **Step 3: Implement the locked, committed claim and state-checked completion helpers.**

- [ ] **Step 4: Run the focused tests and verify they pass.**

### Task 3: Implement tenant-safe image execution and failure classification

**Files:**
- Modify: `database/content_publishing.py`
- Test: `tests/test_phase7c_scheduled_publishing.py`

**Interfaces:**
- Consumes: a session factory, `resolve_oauth_instagram_account_runtime`, and an injectable image provider callable.
- Produces: `execute_due_scheduled_content(session_factory, now, limit, publisher)` summary.

- [ ] **Step 1: Write failing tests for image success, unsupported type, unusable tenant credential, tenant identity mismatch, confirmed provider rejection, and ambiguous request failure.**

```python
summary = execute_due_scheduled_content(Session, now=due_time, publisher=timeout_publisher)
assert summary.failed == 1
assert item.status == "failed"
assert item.provider_request_may_have_been_sent is True
```

- [ ] **Step 2: Run the focused tests and verify they fail because execution does not exist.**

- [ ] **Step 3: Implement the image provider boundary and lifecycle service without retrying any terminal row.**

- [ ] **Step 4: Run the focused tests and verify success, sanitized failures, and repeated execution safety.**

### Task 4: Add the scheduler CLI entry point

**Files:**
- Modify: `database/cli.py`
- Test: `tests/test_phase7c_scheduled_publishing.py`

**Interfaces:**
- Produces: `flask publish-scheduled-content --limit N`.

- [ ] **Step 1: Write a failing CLI test that verifies the command invokes the execution service and prints only a count summary.**

- [ ] **Step 2: Run the focused test and verify it fails because the command is absent.**

- [ ] **Step 3: Register the bounded CLI command using the existing database URL/session factory pattern.**

- [ ] **Step 4: Run the focused test and verify it passes.**

### Task 5: Verify the affected surface

**Files:**
- Test: `tests/test_phase7b_content_scheduling.py`, `tests/test_account_runtime.py`, `tests/test_post_publishing_disabled.py`

- [ ] **Step 1: Run focused Phase 7C tests.**
- [ ] **Step 2: Run affected Phase 7B, OAuth runtime, and legacy-disabled-publisher tests.**
- [ ] **Step 3: Inspect the migration diff, working tree, and untracked files; verify historical migration files are unchanged and no debug artifacts remain.**
