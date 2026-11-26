# Postenza Landing Page — Phase 1 Design

## Goal

Replace the public root health-style response with a polished Postenza marketing page that accurately presents the existing Instagram automation product. The authenticated `/app` workspace, provider integrations, and lifecycle semantics remain unchanged.

## Scope

- Render a new public landing page at `/`.
- Add `/health` as a lightweight liveness response while retaining `/api/v01-health` unchanged.
- Reuse the Postenza assets and existing Flask/Jinja stack.
- Add a responsive, keyboard-accessible navigation menu, product sections, FAQ, CTAs, footer, and basic metadata.
- Extend the existing session-backed `en`, `fr`, and `ar` localization foundation for public landing content, including RTL layout.
- Add a public, same-origin locale preference POST handler that stores only an allowlisted locale in the existing browser session.
- Add focused root/health/landing localization tests.

## Non-goals

- No upload feature, live provider media synchronization, pricing, billing, advanced SEO, new social networks, or production changes.
- No changes to OAuth, publishing, Cron, credentials, schema, tenant isolation, or automation behavior.
- No stock imagery, invented metrics, unsupported platform claims, or unsupported video/reel/carousel claims.

## Architecture

`webhook.py` keeps the public route ownership but delegates landing presentation to `landing.html`. The root route builds template context from `dashboard.i18n` using the current session locale. `/health` returns a small JSON liveness payload and does not inspect credentials, touch provider APIs, or mutate data. The existing readiness endpoint remains intact.

`landing.html` is a self-contained semantic Jinja page that imports only existing Postenza assets and `landing.css`. The hero product visual is a static CSS/Jinja mockup using generic workflow labels; it does not fetch or expose tenant, account, or provider data. A small inline script handles only the accessible mobile navigation toggle.

## Routing and State

| Route | Method | Behavior |
| --- | --- | --- |
| `/` | GET | Public Postenza landing page with active session locale. |
| `/health` | GET | Constant liveness JSON response. |
| `/locale` | POST | Validates a CSRF token, stores an allowlisted locale in the existing session key, and redirects only to `/`. |
| `/api/v01-health` | GET | Unchanged detailed readiness endpoint. |

The locale handler accepts only `en`, `fr`, and `ar`; invalid values normalize to English. It does not accept an arbitrary redirect target.

## Page Structure

1. Sticky public navigation with logo, in-page anchors, locale selector, Login, and Get Started; a keyboard-operable mobile menu is used below the desktop breakpoint.
2. Hero with the approved headline, supporting copy, tagline, CTA pair, and a CSS product mockup.
3. Capability strip: Instagram-focused, scheduled image publishing, automation controls, and multiple accounts.
4. Capability-backed feature cards.
5. Four-step setup/use workflow.
6. Explicit image-only publishing workflow: create, schedule, automatic publish, status tracking.
7. Automation flow showing public reply, optional private follow-up, optional follow-up, and smart replies under owner control.
8. Engagement and account-connection summaries with safe, product-level language.
9. FAQ with only current capabilities.
10. Final CTA and footer with real route links only.

## Localization and RTL

Landing strings are centralized in `dashboard.i18n.TRANSLATIONS`. Product name remains Postenza in all locales. The landing template sets `lang` and `dir` from `locale_context`; CSS uses direction-safe alignment and switches visual ordering for Arabic where needed. The language selector is a standard labeled form control.

## Accessibility, SEO, and Performance

- Semantic header, nav, main, sections, headings, buttons/links, and footer.
- Visible focus styles, contrast-safe dark/navy and white surfaces, meaningful image alt text, and `aria-expanded` mobile menu state.
- Responsive grids collapse without horizontal scrolling.
- Title, description, canonical-ready URL generation, and Open Graph title/description/image metadata are included. No sitemap or advanced SEO is added.
- No external framework, font runtime, stock media, or large dashboard capture is loaded.

## Verification

Focused tests will assert root rendering, liveness behavior, CTA and auth routes, supported capability copy, absence of Sociora and unsupported-platform claims, locale/RTL rendering, and mobile navigation markup. Existing auth and tenant-dashboard tests remain part of affected verification.
