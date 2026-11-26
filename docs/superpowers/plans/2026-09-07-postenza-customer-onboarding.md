# Postenza Customer Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persisted, localized, server-controlled onboarding flow for newly registered customers while preserving legacy-user access and all existing Instagram runtime behavior.

**Architecture:** A new one-to-one `UserOnboardingProfile` model records allowlisted questionnaire answers and completion. Registration inserts the user and empty profile in one transaction; shared read helpers treat a missing profile as complete only for historical users, while `/onboarding` and `/app` derive state from persistence. The existing read-only Admin snapshot gains aggregate plan/source counts without creating plan entitlements.

**Tech Stack:** Python 3, Flask, SQLAlchemy 2, Alembic, Jinja, vanilla CSS, `unittest`, Node test runner.

**Spec:** `docs/superpowers/specs/2026-09-07-postenza-customer-onboarding-design.md`

## Global Constraints

- `selected_plan` is informational only and must not affect product capabilities, account limits, usage limits, billing, or entitlements.
- A missing onboarding profile is backward compatibility for pre-existing users only.
- New browser registration must atomically create both `users` and `user_onboarding_profiles` rows; any failure rolls back both and does not authenticate.
- The server controls step order, validates fixed allowlists, and accepts no client identity or completion flag.
- Admin remains GET-only, read-only, and protected by `current_user_has_legacy_workspace_access()`.
- OAuth exchange, credentials, provider calls, account ownership, Prompt capability, webhook delivery, publishing, and scheduling semantics remain unchanged.
- Do not commit, push, deploy, modify Railway, run production migrations, change environment variables, or perform unrelated cleanup.

---

### Task 1: Persisted onboarding domain and additive migration

**Files:**
- Modify: `database/models.py`
- Create: `database/onboarding.py`
- Create: `alembic/versions/20260907_01_user_onboarding_profiles.py`
- Create: `tests/test_user_onboarding.py`
- Create: `tests/test_user_onboarding_migration.py`

**Interfaces:**
- Produces: `UserOnboardingProfile`; `USE_CASE_VALUES`, `MAIN_GOAL_VALUES`, `ONBOARDING_SOURCE_VALUES`, `PLAN_VALUES`; `onboarding_profile_for_user(session, user_id)`; `user_onboarding_is_complete(session, user_id)`; `expected_onboarding_step(profile)`; `apply_onboarding_step(profile, step, values, now=None)`.
- Missing profiles return `True` from `user_onboarding_is_complete()` as the explicit legacy interpretation; only registration creates new-user profiles.

- [ ] **Step 1: Write failing model/helper tests.**

  Cover literal allowlists, first-incomplete-step order, invalid/out-of-order input rejection without mutation, completion timestamp assignment, authenticated-user scoping, and missing-row legacy completion.

- [ ] **Step 2: Run the focused domain tests and verify RED.**

  Run: `.venv\Scripts\python.exe -m unittest tests.test_user_onboarding -v`

  Expected: import/model failures because the onboarding domain does not exist.

- [ ] **Step 3: Add the minimal model and domain helpers.**

  Add a nullable-answer, one-row-per-user model with bounded strings, timestamps, and cascading user FK. Validate values against the approved literal tuples and update only the expected step.

- [ ] **Step 4: Re-run the focused domain tests and verify GREEN.**

  Run: `.venv\Scripts\python.exe -m unittest tests.test_user_onboarding -v`

- [ ] **Step 5: Write failing migration tests.**

  Exercise upgrade from `20260906_02`, inspect exactly the documented table/columns/FK/nullability, verify no historical user backfill, prove user cascade deletion, prove empty-table downgrade, and prove downgrade aborts when any profile exists.

- [ ] **Step 6: Run the migration tests and verify RED.**

  Run: `.venv\Scripts\python.exe -m unittest tests.test_user_onboarding_migration -v`

- [ ] **Step 7: Add the additive migration and verify GREEN.**

  Use revision `20260907_01`, down revision `20260906_02`, no backfill, and a `SELECT 1 ... LIMIT 1` downgrade guard before dropping the empty table.

  Run: `.venv\Scripts\python.exe -m unittest tests.test_user_onboarding_migration -v`

### Task 2: Atomic registration and destination gating

**Files:**
- Modify: `auth.py`
- Modify: `tenant_dashboard.py`
- Modify: `tests/test_auth.py`
- Modify: `tests/test_tenant_dashboard.py`

**Interfaces:**
- Consumes: `UserOnboardingProfile`, `user_onboarding_is_complete()`.
- Produces: atomic registration invariant and shared login/tenant-entry onboarding gate.

- [ ] **Step 1: Write failing registration tests.**

  Assert successful registration creates exactly one empty profile before authentication. Use a SQLAlchemy insert failure on `UserOnboardingProfile` to prove the response fails safely, the user row is rolled back, no profile remains, and no authenticated session is created.

- [ ] **Step 2: Verify registration tests fail for the missing profile behavior.**

  Run: `.venv\Scripts\python.exe -m unittest tests.test_auth.AuthenticationCoreTest.test_register_creates_user_and_incomplete_onboarding_profile_atomically tests.test_auth.AuthenticationCoreTest.test_registration_profile_failure_rolls_back_user_and_does_not_authenticate -v`

- [ ] **Step 3: Insert `User` and `UserOnboardingProfile` in the existing registration transaction.**

  Flush the user to obtain its UUID, add the profile, flush again inside the same transaction, and rotate the browser session only after commit and reload. Preserve duplicate-email handling and safe SQLAlchemy failure behavior.

- [ ] **Step 4: Write failing login and tenant-entry gate tests.**

  Assert an incomplete-profile user is redirected to `/onboarding` even when connected, direct `/app` requests redirect before account selection, and users with no profile retain legacy access. Assert Admin internal-owner access is unaffected.

- [ ] **Step 5: Add minimal destination and tenant-context gates, then verify GREEN.**

  Make `post_login_destination()` check persistence before connection status. In `_tenant_context()`, check the authenticated user's profile before loading/selecting tenant accounts. Treat database errors as unavailable rather than complete.

  Run: `.venv\Scripts\python.exe -m unittest tests.test_auth tests.test_tenant_dashboard -v`

### Task 3: Server-controlled questionnaire and localized presentation

**Files:**
- Modify: `auth.py`
- Modify: `dashboard/templates/onboarding.html`
- Modify: `dashboard/static/auth.css`
- Modify: `dashboard/app_translations.py`
- Create: `tests/test_onboarding_flow.py`
- Modify: `tests/test_auth_shell.py`
- Modify: `tests/test_onboarding_status.py`

**Interfaces:**
- Consumes: onboarding domain helpers and approved allowlists.
- Produces: GET/POST `/onboarding` state machine and four-step localized UI.

- [ ] **Step 1: Write failing route tests for all questionnaire steps.**

  Cover first-step rendering, valid profile/source/plan transitions, persisted values, required/unknown values, CSRF, out-of-order step rejection, safe submitted-value retention, no mutation on failure, no user/tenant identifiers in forms, completion timestamp, connected/disconnected completion destinations, and tenant isolation.

- [ ] **Step 2: Run the focused flow tests and verify RED.**

  Run: `.venv\Scripts\python.exe -m unittest tests.test_onboarding_flow -v`

- [ ] **Step 3: Implement the minimal POST state machine and context builder.**

  Change `/onboarding` to GET/POST. Load only the current user's profile, derive the expected step server-side, validate CSRF and allowlisted values, persist only that step, and redirect after each success. Render the existing Instagram account status only when the profile is complete or absent for legacy compatibility.

- [ ] **Step 4: Add the four-step template, non-entitlement plan positioning, CSS, and EN/FR/AR messages.**

  Plan cards describe user preference/positioning only, all say `Pricing coming soon`, and explicitly state selection does not change access. Retain native radio inputs, focus styles, responsive layout, account action references, locale selector, logout, and RTL behavior.

- [ ] **Step 5: Verify focused onboarding and shell suites GREEN.**

  Run: `.venv\Scripts\python.exe -m unittest tests.test_onboarding_flow tests.test_onboarding_status tests.test_auth_shell -v`

### Task 4: OAuth entry boundary and post-callback routing

**Files:**
- Modify: `auth.py`
- Modify: `tests/test_instagram_oauth_foundation.py`
- Modify: `tests/test_instagram_oauth_callback.py`

**Interfaces:**
- Consumes: `user_onboarding_is_complete()` and existing OAuth/account-reference helpers.
- Produces: questionnaire-before-connect enforcement and onboarding-aware post-success redirect without changing provider work.

- [ ] **Step 1: Write failing OAuth boundary tests.**

  Prove an incomplete profile cannot forge a connection-start POST or receive OAuth state; legacy missing-profile and complete users retain current start behavior. Prove successful callback keeps the existing account-scoped `/app?account=...` redirect for complete/legacy users and sends an incomplete user back to `/onboarding` only after the unchanged successful connection transaction.

- [ ] **Step 2: Run the exact OAuth tests and verify RED.**

  Run: `.venv\Scripts\python.exe -m unittest tests.test_instagram_oauth_foundation tests.test_instagram_oauth_callback -v`

- [ ] **Step 3: Add only the completion checks around existing OAuth orchestration.**

  Reject direct connection initiation before any state/configuration work. After successful callback persistence, choose the onboarding destination if still incomplete; otherwise preserve the signed selected-account redirect and public-base handling.

- [ ] **Step 4: Re-run OAuth tests and verify GREEN.**

  Run: `.venv\Scripts\python.exe -m unittest tests.test_instagram_oauth_foundation tests.test_instagram_oauth_callback tests.test_instagram_connection_callback tests.test_instagram_connection_persistence -v`

### Task 5: Real read-only Admin distributions

**Files:**
- Modify: `database/admin_dashboard.py`
- Modify: `dashboard/templates/app_admin.html`
- Modify: `tests/test_admin_dashboard.py`

**Interfaces:**
- Consumes: onboarding allowlists and `UserOnboardingProfile`.
- Produces: `AdminSnapshot.plan_distribution` and `.onboarding_source_attribution` as ordered immutable `(label/value, count)` aggregates including unknown.

- [ ] **Step 1: Write failing Admin aggregate tests.**

  Seed plan/source combinations, missing profiles, null values, and unexpected database values. Assert each distribution totals registered users, known counts are accurate, all absent/unknown answers are grouped as `Unknown / not provided`, credentials remain absent, access remains internal-only, POST stays 405, and GET changes no rows.

- [ ] **Step 2: Verify Admin tests RED.**

  Run: `.venv\Scripts\python.exe -m unittest tests.test_admin_dashboard -v`

- [ ] **Step 3: Implement aggregate-only queries and truthful rendering.**

  Outer-join users to profiles, group raw answer values, fold only allowlisted keys into named buckets, and render simple counts. Keep active users unavailable and do not load full profile, credential, provider, or automation records.

- [ ] **Step 4: Re-run Admin tests and verify GREEN.**

  Run: `.venv\Scripts\python.exe -m unittest tests.test_admin_dashboard -v`

### Task 6: Broad compatibility and final verification

**Files:**
- Verify all files above; do not add unrelated files.

**Interfaces:**
- Produces: final evidence that the bounded patch is safe to commit, without committing it.

- [ ] **Step 1: Run focused feature/security suites.**

  Run: `.venv\Scripts\python.exe -m unittest tests.test_user_onboarding tests.test_user_onboarding_migration tests.test_onboarding_flow tests.test_auth tests.test_auth_shell tests.test_onboarding_status tests.test_admin_dashboard tests.test_tenant_dashboard tests.test_account_ownership -v`

- [ ] **Step 2: Run OAuth/provider regression suites.**

  Run: `.venv\Scripts\python.exe -m unittest tests.test_instagram_oauth_foundation tests.test_instagram_oauth_callback tests.test_instagram_connection_callback tests.test_instagram_connection_persistence tests.test_oauth_engagement tests.test_webhook_tenant_routing -v`

- [ ] **Step 3: Run the full Python suite and JavaScript tests.**

  Run: `.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -q`

  Run: `node --test tests/*.test.js`

- [ ] **Step 4: Compile every Jinja template and validate localization parity.**

  Parse all `dashboard/templates/*.html` with `jinja2.Environment`; assert every literal `t()` key exists in EN/FR/AR and compare format placeholders for every catalog row.

- [ ] **Step 5: Compile Python and validate the canonical patch.**

  Run: `.venv\Scripts\python.exe -m compileall -q auth.py tenant_dashboard.py database tests alembic/versions`

  Run: `git diff --check`

  Run: `git status --short`

  Run: `git diff --stat`

- [ ] **Step 6: Report exact results and stop.**

  Return the persisted fields, access/gating behavior, informational-plan confirmation, Admin metrics and unknown state, exact files changed, test/check totals, diff stat/status, and safe-to-commit verdict. Do not commit, push, deploy, or modify external state.
