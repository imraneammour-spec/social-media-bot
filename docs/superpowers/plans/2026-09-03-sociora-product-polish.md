# Sociora Product Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the approved Sociora identity, correct selected-account automation status, and polish the existing tenant dashboard without changing provider, publishing, credential, schema, or Cron behavior.

**Architecture:** Keep all routes and persisted models intact. Add a small locale module backed by the existing Flask session, expose locale metadata through the tenant dashboard template context, and use sanitized view-model data for presentation-only feeds. Existing public HTTPS image URLs and persisted prompt-post data remain the only content/media sources.

**Tech Stack:** Flask, Jinja, SQLAlchemy, unittest, CSS.

**Spec:** User-approved Sociora product-polish/generalization brief in this conversation.

## Global Constraints

- Use the approved assets in `dashboard/static/brand/` exactly; do not generate or alter logos.
- Do not change OAuth, publishing lifecycle, Cron, credentials, tenant isolation, database schema, or provider requests.
- Do not implement image uploads or provider media sync; make current UI scope honest.
- Do not commit, push, deploy, or change production.

---

### Task 1: Brand and locale template foundation

**Files:**
- Create: `dashboard/i18n.py`
- Modify: `tenant_dashboard.py`, `dashboard/templates/app_base.html`, `dashboard/templates/login.html`, `dashboard/templates/register.html`, `dashboard/templates/onboarding.html`, `dashboard/static/tenant_dashboard.css`
- Test: `tests/test_tenant_dashboard.py`, `tests/test_auth.py`

- [ ] Write failing render tests for Sociora logo/favicon, locale selection, and RTL document direction.
- [ ] Implement session-backed locale normalization and centralized English, French, and Arabic labels.
- [ ] Add locale selection endpoints/forms without changing user or account persistence.
- [ ] Render approved logos, favicon links, translated browser identity, and responsive RTL styles.
- [ ] Run the focused tests.

### Task 2: Selected-account automation summary and overview feed

**Files:**
- Modify: `database/tenant_dashboard.py`, `tenant_dashboard.py`, `dashboard/templates/app_overview.html`, `dashboard/static/tenant_dashboard.css`
- Test: `tests/test_tenant_dashboard.py`

- [ ] Write a failing account-scoped overview test where one automation config is enabled and another is disabled.
- [ ] Read the selected account's existing `InstagramAutomationConfig` in the overview view model.
- [ ] Replace raw activity labels with sanitized, human-facing feed fields.
- [ ] Render compact activity entries with local-readable timestamps and icons.
- [ ] Run focused dashboard tests.

### Task 3: Content, calendar, and engagement presentation pass

**Files:**
- Modify: `dashboard/templates/app_content.html`, `dashboard/templates/app_scheduler.html`, `dashboard/templates/app_leads.html`, `dashboard/templates/app_activity.html`, `dashboard/templates/app_messages.html`, `dashboard/templates/app_followups.html`, `dashboard/templates/app_automations.html`, `dashboard/templates/app_accounts.html`, `dashboard/static/tenant_dashboard.css`, `database/tenant_dashboard.py`
- Test: `tests/test_tenant_dashboard.py`, `tests/test_phase7b_content_scheduling.py`, `tests/test_tenant_lead_management.py`

- [ ] Write failing render tests for generalized copy, honest persisted-post labeling, and existing action controls.
- [ ] Rename only presentation labels from prompt-centric terms to generic engagement language.
- [ ] Keep Content lifecycle management and Calendar chronological-only.
- [ ] Improve lead detail sections, filters, pagination, empty states, controls, and responsive action rows using existing form actions and opaque references.
- [ ] Run affected focused suites.

### Task 4: Verification and deferred architecture record

**Files:**
- Modify: `docs/superpowers/plans/2026-09-03-sociora-product-polish.md`
- Test: all affected suites

- [ ] Run compile checks for changed Python modules.
- [ ] Run `git diff --check`.
- [ ] Record that upload requires durable public HTTPS object storage and that refresh requires a bounded account-scoped Graph read and idempotent persistence design.

## Deferred architecture decisions

- **Image upload:** The current image-only publisher accepts a public HTTPS `media_url`. A real upload flow requires durable object storage with tenant-safe ownership, an application-controlled public HTTPS delivery URL that Meta can fetch, file validation, lifecycle cleanup, and a defined retention policy. No storage provider or secure upload boundary exists in this project today.
- **Instagram media refresh:** The current recorded-post list is based on local `InstagramPromptPost` records, not a complete provider inventory. A later refresh phase should add a bounded Graph media read for one selected account, resolve credentials through the existing encrypted runtime path, deduplicate using provider media IDs, and define a narrow local persistence model before the UI calls it a complete list.
