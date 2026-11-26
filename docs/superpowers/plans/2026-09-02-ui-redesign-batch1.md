# UI Redesign Batch 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a unified, responsive, accessible visual foundation for the canonical `/app` workspace, including Overview and image-only Content.

**Architecture:** Keep Flask/Jinja server rendering and all existing routes and mutation semantics. Refactor the tenant stylesheet around semantic tokens and reusable CSS classes, then reshape `app_base.html`, Overview, and Content to consume those classes. Add render-level tests for the user-visible contracts; do not alter publishing, OAuth, credentials, automation, or schema behavior.

**Tech Stack:** Flask, Jinja, CSS custom properties, unittest, SQLAlchemy test fixtures.

**Spec:** Audit approved in the 2026-09-02 UI/UX audit conversation.

## Global Constraints

- `/app` is the canonical redesigned authenticated workspace; leave legacy `/dashboard` functional and visually untouched.
- Use `Social Media Bot` in redesigned authenticated UI.
- Image is the only active content type; unsupported types must not be interactive options.
- Preserve tenant isolation, CSRF, publishing lifecycle, OAuth, credentials, Cron, automation behavior, and database schema.
- Do not deploy, commit, or push in this batch.

---

### Task 1: Establish render contracts for Batch 1

**Files:**

- Modify: `tests/test_tenant_dashboard.py`
- Modify: `tests/test_messages_content.py`

**Interfaces:**

- Consumes: existing Flask tenant-dashboard fixture and `/app` routes.
- Produces: render-level coverage for canonical navigation, accessible account/logout controls, Overview state hierarchy, and image-only Content controls.

- [ ] **Step 1: Write failing tests**

```python
page = self.client.get("/app").get_data(as_text=True)
self.assertIn('aria-label="Primary navigation"', page)
self.assertIn('>Overview</a>', page)
self.assertIn('>Calendar</a>', page)
self.assertNotIn('>Leads</a>', page)

content_page = self.client.get(f"/app/content?account={account_ref}").get_data(as_text=True)
self.assertIn('name="content_type" value="image"', content_page)
self.assertNotIn('<option value="video">', content_page)
```

- [ ] **Step 2: Run the targeted tests and confirm they fail because the old shell/content markup remains.**

Run: `python -m unittest tests.test_tenant_dashboard.TenantDashboardTest.test_authenticated_oauth_user_sees_shell_with_safe_navigation tests.test_messages_content.MessagesContentDashboardTest.test_content_empty_state_works -v`

- [ ] **Step 3: Add minimal render assertions for user-visible behavior**

Keep tests against rendered HTML, not source-code strings. Seed existing content states to verify Draft, Scheduled, Published, and Needs attention labels and a safe failure message.

- [ ] **Step 4: Re-run targeted tests after implementation**

Run: `python -m unittest tests.test_tenant_dashboard tests.test_messages_content tests.test_phase7b_content_scheduling -v`

### Task 2: Build the shared app shell and tokenized stylesheet

**Files:**

- Modify: `dashboard/static/tenant_dashboard.css`
- Modify: `dashboard/templates/app_base.html`

**Interfaces:**

- Consumes: `active_page`, `selected_account`, `accounts`, `current_user`, `account_switch_endpoint`, and `csrf_token` already supplied by `_render_page`.
- Produces: semantic CSS tokens; reusable button/card/badge/alert/form/tab/list classes; desktop sidebar and mobile drawer navigation.

- [ ] **Step 1: Use the failing shell test from Task 1.**
- [ ] **Step 2: Replace the existing dark one-off visual layer with semantic CSS variables and reusable classes.**

Use the approved canvas, surface, sidebar, primary, accent, text, border, success, warning, and error colors. Add visible focus styling and a `768px` mobile drawer breakpoint. Do not add JavaScript framework dependencies.

- [ ] **Step 3: Rebuild `app_base.html`.**

Render exactly six primary navigation items: Overview, Content, Calendar, Engagement, Automation, and Instagram. Retain existing route targets behind their matching primary item; expose non-primary operational routes through secondary page controls later. Add responsive menu toggle markup, selected-account context, and a CSRF-protected logout form.

- [ ] **Step 4: Re-run shell and stylesheet tests.**

Run: `python -m unittest tests.test_tenant_dashboard -v`

### Task 3: Redesign Overview and Content around the verified publishing workflow

**Files:**

- Modify: `dashboard/templates/app_overview.html`
- Modify: `dashboard/templates/app_content.html`
- Modify: `tests/test_tenant_dashboard.py`
- Modify: `tests/test_messages_content.py`

**Interfaces:**

- Consumes: existing sanitized overview and content payloads plus owned opaque action references.
- Produces: clear publishing states and image-only composition without changing routes or mutations.

- [ ] **Step 1: Use the failing state-label and image-only tests from Task 1.**
- [ ] **Step 2: Rebuild Overview with existing data only.**

Show connection state, draft count, automation state, safe “needs attention” indication when the connection requires attention, and clear links to create or schedule content. Do not add analytics queries or provider calls.

- [ ] **Step 3: Rebuild Content.**

Use an image-only composer with an image URL preview container, caption, Save draft, and schedule controls. Group existing rows into Drafts, Scheduled, Published, and Needs attention. Translate internal lifecycle names to user labels, preserve opaque action references, show no provider codes, and never add an automatic retry action.

- [ ] **Step 4: Run focused behavior and regression tests.**

Run: `python -m unittest tests.test_messages_content tests.test_phase7b_content_scheduling tests.test_tenant_dashboard -v`

### Task 4: Verify presentation safety and repository integrity

**Files:**

- Verify: all modified Batch 1 files

- [ ] **Step 1: Compile Python files affected by tests or presentation support.**

Run: `python -m compileall -q tenant_dashboard.py tests/test_tenant_dashboard.py tests/test_messages_content.py`

- [ ] **Step 2: Run CSS/template render checks through existing unittest suites.**

Run: `python -m unittest tests.test_tenant_dashboard tests.test_messages_content tests.test_phase7b_content_scheduling -v`

- [ ] **Step 3: Inspect formatting and status.**

Run: `git diff --check` and `git status --short`
