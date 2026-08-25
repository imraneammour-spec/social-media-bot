# Phase 3A Authentication Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add secure database-backed registration, login, logout, and temporary legacy-workspace isolation without changing live JSON operations.

**Architecture:** A focused `auth` module will own session configuration, password validation, server-side current-user lookup, CSRF, temporary rate limiting, and legacy owner checks. `webhook.py` will register auth routes and apply a centralized private-route gate; it will keep webhook/public paths public and keep JSON-backed operations unchanged.

**Tech Stack:** Flask signed sessions, Werkzeug password hashing, SQLAlchemy, and PostgreSQL in deployment with SQLite for isolated tests.

**Spec:** User-approved Phase 3A authentication specification supplied in this conversation.

## Global Constraints

- Do not commit, push, deploy, or start Phase 3B.
- Do not migrate operational JSON data or store Instagram credentials/tokens.
- Post publishing and scheduling remain disabled; Story automation and scheduler remain active.
- Only the Phase 2 database owner of the legacy `INSTAGRAM_USER_ID` may use legacy workspace routes.
- New users may authenticate but may access only onboarding until Instagram connection/multi-tenancy work is approved.
- `FLASK_SECRET_KEY` has no production fallback and secrets are never logged.
- `META_APP_SECRET` enables HMAC SHA-256 verification when present; absence preserves webhook compatibility but is surfaced as insecure System Health state.

---

### Task 1: Authentication core and security primitives

**Files:** Create `auth.py`; test `tests/test_auth.py`.

- [ ] Write failing tests for invalid/deleted UUID sessions, signed CSRF validation, secret/database unavailable behavior, and legacy-owner lookup.
- [ ] Implement lazy database access, safe UUID lookup, secure cookie configuration, bounded session lifetime, random session-tied CSRF tokens, and rate limiting.
- [ ] Run the focused tests to green.

### Task 2: Authentication and onboarding routes

**Files:** Modify `webhook.py`; create `dashboard/templates/login.html`, `register.html`, and `onboarding.html`; test `tests/test_auth.py`.

- [ ] Write failing tests for registration, duplicate email, password mismatch, generic login failure, successful login, logout, CSRF rejection, and database-unavailable responses.
- [ ] Implement normalized input validation, Werkzeug hashes, session rotation, POST-only logout, and accessible dark templates.
- [ ] Run focused route tests to green.

### Task 3: Legacy workspace isolation

**Files:** Modify `webhook.py` and `dashboard/templates/dashboard.html`; test `tests/test_auth.py`.

- [ ] Write failing tests for dashboard redirect, owner access, regular-user onboarding, private API `401`/`403`, public routes, and webhook accessibility.
- [ ] Implement centralized route classification, owner-only legacy access, API CSRF headers, and minimal identity/logout UI.
- [ ] Run boundary and dashboard tests to green.

### Task 4: Webhook hardening and health status

**Files:** Modify `webhook.py`; test `tests/test_auth.py`.

- [ ] Write failing tests for valid/invalid Meta signatures and compatibility without an app secret.
- [ ] Implement constant-time HMAC signature verification, explicit missing `VERIFY_TOKEN` behavior, and System Health reporting of active/inactive verification.
- [ ] Run webhook tests to green.

### Task 5: Full verification and scope review

- [ ] Run compilation, auth tests, database foundation tests, scheduler tests, post-publishing-disabled tests, dashboard JavaScript tests, and `git diff --check`.
- [ ] Review the complete diff for secrets, JSON migration, post publishing/scheduling changes, and scope violations.
- [ ] Produce the Phase 3A report without committing, pushing, deploying, or starting Phase 3B.
