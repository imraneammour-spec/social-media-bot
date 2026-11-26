# UI Redesign Batch 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the Calendar, Instagram, Automation, and Engagement areas using the approved Batch 1 authenticated workspace system.

**Architecture:** Preserve the existing Flask routes and mutation handlers. Templates consume the existing sanitized view payloads; the Calendar route only broadens its account-scoped content presentation data to existing lifecycle states and filters it for display. Shared CSS supplies agenda, tabs, settings, and account-card patterns across the redesigned pages.

**Tech Stack:** Flask/Jinja, SQLAlchemy presentation queries, vanilla CSS, Python unittest.

**Spec:** User-approved Batch 2 design in this conversation (2026-09-03).

## Global Constraints

- Do not change publishing lifecycle, Cron, OAuth, credentials, tenant isolation, automation semantics, routes, mutations, or schema.
- Keep the Batch 1 dark sidebar, light workspace, purple primary, semantic status pills, and responsive drawer behavior.
- Use existing sanitized account and content data only; never render provider errors, OAuth stages, identifiers, or credential data.
- Do not add active unsupported content types, landing-page/SEO work, production changes, or deployment configuration changes.

---

### Task 1: Calendar publishing agenda

**Files:**
- Modify: `tenant_dashboard.py:1397-1456`
- Modify: `dashboard/templates/app_scheduler.html`
- Modify: `dashboard/static/tenant_dashboard.css`
- Test: `tests/test_phase7b_content_scheduling.py`

**Interfaces:**
- Consumes: `tenant_data.content_drafts(session, instagram_account_id=...)` sanitized content records and existing `scheduler_jobs` data.
- Produces: `agenda_content` limited to `scheduled`, `published`, and `failed`, plus `selected_content_status` limited to `all`, `scheduled`, `published`, and `failed`.

- [ ] **Step 1: Write failing render tests** for status-filter links and safe user-facing Scheduled, Published, and Needs attention rows.
- [ ] **Step 2: Run those tests** and confirm the current scheduler template lacks the agenda/filter presentation.
- [ ] **Step 3: Add the bounded account-scoped presentation payload** and render the content agenda before the secondary automation queue.
- [ ] **Step 4: Add shared agenda/filter CSS** that becomes one-column cards on mobile.
- [ ] **Step 5: Re-run Calendar and scheduling tests.**

### Task 2: Instagram connection management

**Files:**
- Modify: `dashboard/templates/app_accounts.html`
- Modify: `dashboard/static/tenant_dashboard.css`
- Test: `tests/test_tenant_dashboard.py`

**Interfaces:**
- Consumes: existing sanitized `accounts` connection labels/details and pre-existing `can_reconnect`, `can_disconnect`, and CSRF action fields.
- Produces: clear account state cards and a client-side, keyboard-accessible disconnect confirmation before the existing form is submitted.

- [ ] **Step 1: Write failing tests** for connected/problem labels and confirmation markup without provider/OAuth diagnostics.
- [ ] **Step 2: Run the tests** and confirm the legacy deployment-oriented account surface fails them.
- [ ] **Step 3: Replace only account-page presentation** with user-facing connection cards and preserve existing reconnect/disconnect forms.
- [ ] **Step 4: Add generic account-card and confirmation styles.**
- [ ] **Step 5: Re-run account template tests.**

### Task 3: Outcome-oriented automation controls

**Files:**
- Modify: `dashboard/templates/app_automations.html`
- Modify: `dashboard/static/tenant_dashboard.css`
- Test: `tests/test_tenant_dashboard.py`

**Interfaces:**
- Consumes: existing `automation` fields and `smart_reply_tones`; form field names and POST target remain unchanged.
- Produces: grouped public/private/smart-reply settings with explanatory copy and enabled/disabled labels.

- [ ] **Step 1: Write failing template tests** for grouped controls and existing named inputs.
- [ ] **Step 2: Run the tests** and confirm the technical step-oriented copy fails them.
- [ ] **Step 3: Rebuild the template around existing inputs** without changing field names, values, or mutation semantics.
- [ ] **Step 4: Add generic settings-group styles and responsive form behavior.**
- [ ] **Step 5: Re-run automation tests.**

### Task 4: Unified Engagement navigation and views

**Files:**
- Modify: `dashboard/templates/app_leads.html`
- Modify: `dashboard/templates/app_messages.html`
- Modify: `dashboard/templates/app_activity.html`
- Modify: `dashboard/templates/app_followups.html`
- Modify: `dashboard/static/tenant_dashboard.css`
- Test: `tests/test_messages_content.py`, `tests/test_tenant_dashboard.py`

**Interfaces:**
- Consumes: existing account selection references, scoped filter data, opaque action references, and form routes.
- Produces: a shared links-only Engagement tab bar on each existing route; the sidebar remains the only primary Engagement entry.

- [ ] **Step 1: Write failing tests** for the four accessible tab links and shared active state.
- [ ] **Step 2: Run the tests** and confirm current separate surfaces lack the shared tabs.
- [ ] **Step 3: Add the tab markup to each template** while preserving filters, pagination, and action forms.
- [ ] **Step 4: Apply existing cards/list rows plus generic tab styles.**
- [ ] **Step 5: Re-run messages, tenant, and scheduler regression suites.**

### Task 5: Final verification

**Files:**
- Verify: all modified files

- [ ] **Step 1: Run focused UI/template tests** for Calendar, Accounts, Automation, and Engagement.
- [ ] **Step 2: Run affected existing suites**: `tests.test_tenant_dashboard`, `tests.test_messages_content`, and `tests.test_phase7b_content_scheduling`.
- [ ] **Step 3: Compile changed Python files** if the Calendar route changes.
- [ ] **Step 4: Run** `git diff --check` **and inspect** `git status --short`.
