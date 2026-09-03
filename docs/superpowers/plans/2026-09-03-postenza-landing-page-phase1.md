# Postenza Landing Page — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a localized, responsive Postenza public marketing page at `/` without altering authenticated product behavior.

**Architecture:** `webhook.py` owns the minimal public routes and supplies locale-aware context from `dashboard.i18n`. A dedicated Jinja template and CSS file render the marketing UI and a static product mockup. Tests exercise routes through Flask without provider calls or database mutation.

**Tech Stack:** Flask, Jinja, existing `dashboard.i18n`, CSS, Python `unittest`.

**Spec:** `docs/superpowers/specs/2026-09-03-postenza-landing-page-phase1-design.md`

## Global Constraints

- Keep `/app`, OAuth, publishing, Cron, credentials, tenant isolation, database schema, and automation semantics unchanged.
- Use real current Postenza capabilities only; publishing is image-only.
- Use existing Postenza assets; do not use stock imagery, fake claims, billing, or new social platforms.
- Keep locale values restricted to `en`, `fr`, and `ar`; product name is Postenza in every locale.
- Do not commit, push, deploy, or modify production.

---

### Task 1: Public route and localization contract

**Files:**
- Modify: `dashboard/i18n.py`
- Modify: `webhook.py`
- Create: `tests/test_landing_page.py`

**Interfaces:**
- Consumes: `locale_context(value)`, `normalize_locale(value)`, `translate(locale, key)`, `LOCALE_SESSION_KEY`.
- Produces: `GET /`, `GET /health`, and `POST /locale` with safe locale/session behavior.

- [ ] **Step 1: Write the failing route tests**

```python
def test_root_renders_the_postenza_landing_page(self):
    page = self.client.get("/").get_data(as_text=True)
    self.assertIn("Put your Instagram on autopilot.", page)
    self.assertIn("Postenza", page)

def test_health_is_a_safe_liveness_response(self):
    response = self.client.get("/health")
    self.assertEqual(response.status_code, 200)
    self.assertEqual(response.get_json(), {"status": "ok"})

def test_landing_locale_uses_the_existing_session_preference(self):
    token = self._csrf_for("/")
    self.client.post("/locale", data={"locale": "ar", "csrf_token": token})
    page = self.client.get("/").get_data(as_text=True)
    self.assertIn('lang="ar" dir="rtl"', page)
```

- [ ] **Step 2: Run the route tests and verify they fail**

Run: `python -m unittest tests.test_landing_page`

Expected: root still contains the legacy health-style response and `/health` is absent.

- [ ] **Step 3: Implement the minimal safe route/context layer**

```python
@app.get("/health")
def health():
    return {"status": "ok"}, 200

@app.post("/locale")
def landing_locale():
    if not validate_csrf_token(request.form.get("csrf_token")):
        abort(400)
    session[LOCALE_SESSION_KEY] = normalize_locale(request.form.get("locale"))
    return redirect(url_for("home"))
```

Add only landing translation keys to each existing locale dictionary, then pass `locale`, `locale_options`, and `t` to `landing.html` from `home`.

- [ ] **Step 4: Run the route tests and verify they pass**

Run: `python -m unittest tests.test_landing_page`

Expected: all tests pass.

### Task 2: Semantic landing page and product mockup

**Files:**
- Create: `dashboard/templates/landing.html`
- Create: `dashboard/static/landing.css`
- Modify: `tests/test_landing_page.py`

**Interfaces:**
- Consumes: `locale`, `locale_options`, and callable `t(key)` from the root route.
- Produces: semantic public navigation, sections, translations, safe CTA links, and accessible mobile navigation markup.

- [ ] **Step 1: Write failing content and safety assertions**

```python
def test_landing_includes_real_capabilities_and_safe_ctas(self):
    page = self.client.get("/").get_data(as_text=True)
    for text in ("Schedule content", "Automate replies", "Private follow-ups", "Smart replies"):
        self.assertIn(text, page)
    self.assertIn('href="/register"', page)
    self.assertIn('href="/login"', page)
    self.assertNotIn("TikTok", page)
    self.assertNotIn("Sociora", page)

def test_landing_has_accessible_mobile_navigation_markup(self):
    page = self.client.get("/").get_data(as_text=True)
    self.assertIn('aria-controls="landing-navigation"', page)
    self.assertIn('aria-expanded="false"', page)
```

- [ ] **Step 2: Run the landing content tests and verify they fail**

Run: `python -m unittest tests.test_landing_page.LandingPageTest.test_landing_includes_real_capabilities_and_safe_ctas tests.test_landing_page.LandingPageTest.test_landing_has_accessible_mobile_navigation_markup`

Expected: fail because the legacy root response has no landing markup.

- [ ] **Step 3: Implement the template and CSS**

Build header/nav, hero, value strip, features, workflow, image-only publishing section, automation flow, engagement and connection sections, FAQ, CTA, and footer. Use generic CSS cards in the hero mockup rather than account data or external images. Include local Postenza assets and title/meta/OG tags.

- [ ] **Step 4: Run the landing tests and verify they pass**

Run: `python -m unittest tests.test_landing_page`

Expected: all tests pass.

### Task 3: Responsive, RTL, and regression verification

**Files:**
- Modify: `dashboard/static/landing.css`
- Modify: `tests/test_landing_page.py`

**Interfaces:**
- Consumes: semantic landing markup and locale direction attributes.
- Produces: breakpoint-safe layout, direction-safe controls, focus treatment, and metadata assertions.

- [ ] **Step 1: Write failing RTL and metadata tests**

```python
def test_landing_localizes_french_and_arabic_without_translating_product_name(self):
    for locale, expected in (("fr", "Mettez votre Instagram en pilote automatique."), ("ar", "ضع إنستغرام الخاص بك على الطيار الآلي.")):
        page = self._page_for_locale(locale)
        self.assertIn(expected, page)
        self.assertIn("Postenza", page)

def test_landing_has_basic_seo_metadata(self):
    page = self.client.get("/").get_data(as_text=True)
    self.assertIn('name="description"', page)
    self.assertIn('property="og:title"', page)
    self.assertIn('rel="canonical"', page)
```

- [ ] **Step 2: Run the targeted tests and verify they fail**

Run: `python -m unittest tests.test_landing_page`

Expected: fail until translated keys and metadata are rendered.

- [ ] **Step 3: Complete styles and translation coverage**

Use mobile-first CSS with a desktop grid, `:focus-visible` styling, reduced visual density below the navigation breakpoint, and `html[dir="rtl"]` layout rules. Add complete user-visible landing keys in English, French, and Arabic and render the metadata from translations.

- [ ] **Step 4: Run affected verification**

Run: `python -m unittest tests.test_landing_page tests.test_auth tests.test_tenant_dashboard`

Expected: all affected tests pass.

- [ ] **Step 5: Run syntax and diff validation**

Run: `python -m py_compile webhook.py dashboard/i18n.py; git diff --check`

Expected: both commands exit 0.
